"""ds-bridge: one worker call: opencode run, the live event stream, speeds, retries, checks and fix rounds."""
import hashlib, json, os, signal, subprocess, sys, time, argparse, shutil, queue, threading, fcntl
import fnmatch, re, tempfile, base64, collections, urllib.request, urllib.parse, secrets
import contextlib, io
from .core import *  # noqa: F401,F403
from .workspace import *  # noqa: F401,F403
PROGRESS = os.path.join(STATE, "progress")
SERVICE_JSON = os.path.join(HOME, ".local/state/opencode/service.json")
FIX_MIN_S = 60
WRAPUP_MSG = (
    "SOFT DEADLINE: stop working now and do not start anything new. Write your final "
    "report block immediately. STATUS: DONE only if every part of the task is finished; "
    "otherwise list each unfinished or unverified part under MISSING.")

OVERLOAD_MSG = (
    "Your previous request failed because the model provider was overloaded (not "
    "your fault). Continue the task where you left off: check what is already done "
    "before redoing anything, then reply with the usual final report block.")


def new_session_id():
    return "ses_ds" + secrets.token_hex(10)


def cleanup_progress():
    try:
        names = os.listdir(PROGRESS)
    except OSError:
        return
    cutoff = time.time() - 86400
    stale = time.time() - 120
    for name in names:
        p = os.path.join(PROGRESS, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                continue
        except OSError:
            pass
        try:
            obj = json.load(open(p))
        except (OSError, ValueError):
            continue
        if obj.get("state") != "running":
            continue
        pid = obj.get("pid")
        dead = (not pid_alive(pid)) if pid else obj.get("updated", 0) < stale
        if dead:
            try:
                os.remove(p)
            except OSError:
                pass


def mark_interrupted(jid):
    """Rewrite a still-running progress file as error/interrupted."""
    path = os.path.join(PROGRESS, jid + ".json")
    try:
        obj = json.load(open(path))
    except (OSError, ValueError):
        return
    if obj.get("state") != "running":
        return
    started = obj.get("started") or time.time()
    obj.update({"state": "error", "last": "interrupted",
                "secs": max(0, round(time.time() - started)),
                "updated": time.time(), "pid": os.getpid()})
    try:
        tmp = path + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except OSError:
        pass


def service_auth():
    """Return (service base url, Authorization header) from service.json, or None."""
    try:
        with open(SERVICE_JSON) as f:
            svc = json.load(f)
        url = str(svc["url"]).rstrip("/")
        password = str(svc["password"])
        auth = base64.b64encode(f"opencode:{password}".encode()).decode()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return url, "Basic " + auth


def interrupt_session(sid):
    """Ask the local OpenCode service to stop a session. True on success."""
    if not sid:
        return False
    got = service_auth()
    if not got:
        return False
    url, auth = got
    try:
        req = urllib.request.Request(
            f"{url}/api/session/{urllib.parse.quote(str(sid), safe='')}/interrupt",
            data=b"", method="POST",
            headers={"Authorization": auth, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            resp.read()
        return True
    except Exception:
        return False


def open_event_stream():
    """Open the local OpenCode SSE event stream, or return None if unavailable."""
    got = service_auth()
    if not got:
        return None
    url, auth = got
    req = urllib.request.Request(url + "/api/event", headers={
        "Accept": "text/event-stream",
        "Authorization": auth,
    })
    resp = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        resp = opener.open(req, timeout=3)
        # urllib's timeout also applies to reads; the connection is up now,
        # so clear it to make the consumer's readline() truly blocking.
        try:
            resp.fp.raw._sock.settimeout(None)
        except (AttributeError, OSError):
            pass
        return resp
    except Exception:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        return None


def stderr_tail(path, limit=1000):
    """Read at most the last `limit` characters of a file, as one line."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read().decode("utf-8", "replace")
        return " ".join(data.split())
    except OSError:
        return ""


def _rate(tokens, ms):
    return round(tokens / (ms / 1000)) if ms else None


def set_speeds(res):
    """Measured speeds, from the provider's exact per-step token counts and OpenCode's event
    times (nothing estimated):
    dec_tok_s  decode speed. OpenCode >= 2.3 sends session.step.first-token and
               session.step.streamed (server times of the provider's first output event and the
               end of the response body), so every step counts output + reasoning tokens over
               streamed - first-token: the same formula as OpenCode's footer and DeepSeek's own
               harness (tok_src "opencode"). Older OpenCode: tokens / first-to-last streamed SSE
               event, only for steps whose reasoning was streamed (tok_src "sse").
    thr_tok_s  throughput: all generated tokens (output + reasoning) / model time, from the start
               of each request to its last generation event, so it includes the wait for the
               first token; defined for every model.
    tok_s      the one shown: decode when it covers at least 80% of the tokens, else throughput
               (tok_kind says which)."""
    thr_t, dec_t = res.get("thr_tokens") or 0, res.get("dec_tokens") or 0
    res["thr_tok_s"] = _rate(thr_t, res.get("thr_ms") or 0)
    res["dec_tok_s"] = _rate(dec_t, res.get("dec_ms") or 0)
    from_oc = bool(res.get("ft_steps")) and not res.get("sse_steps")
    use_dec = res["dec_tok_s"] is not None and (from_oc or dec_t >= 0.8 * thr_t)
    res["tok_s"] = res["dec_tok_s"] if use_dec else res["thr_tok_s"]
    res["tok_kind"] = "decode" if use_dec else "throughput" if res["tok_s"] is not None else None
    res["tok_src"] = ("opencode" if from_oc else "sse") if res["tok_s"] is not None else None
    return res


SPEED_KEYS = ("thr_tokens", "thr_ms", "dec_tokens", "dec_ms", "ft_steps", "sse_steps")


def _live_rate(segs, now_ms, cpt):
    """Live tok/s from active generation segments clipped to the last 5 s."""
    win_start = now_ms - 5000
    chars = dur_ms = 0.0
    keep = []
    for start, end, ch in segs:
        if end < win_start - 10000:
            continue
        keep.append((start, end, ch))
        if end <= win_start or start >= now_ms:
            continue
        span = end - start
        lo, hi = max(start, win_start), min(end, now_ms)
        if span <= 0 or hi <= lo:
            continue
        overlap = hi - lo
        chars += ch * overlap / span
        dur_ms += overlap
    if dur_ms >= 300 and chars:
        return int(round((chars / cpt) / (dur_ms / 1000))), keep
    return None, keep


class LiveMeter:
    """What OpenCode's event stream says about one session while it runs: exact token counts per
    finished step, when generation started and stopped, which tools are running. Speeds come from
    here (see set_speeds); the count for a step still streaming is an estimate from characters."""

    def __init__(self):
        self.lock = threading.Lock()
        self.available = False          # the stream is connected
        self.resp = None
        self.segs = []                  # (start_ms, end_ms, chars) of active generation, for the live rate
        self.cpt = 3.5                  # characters per token, calibrated on finished steps
        self.cal_chars = self.cal_tokens = 0
        self.step_chars = self.reason_chars = 0
        self.done_out = 0               # exact output tokens of finished steps
        self.last_delta_ms = self.last_gen_ms = None
        self.step_start_ms = self.first_gen_ms = self.last_step_gen_ms = None
        self.first_tok_ms = self.streamed_ms = None   # OpenCode >= 2.3 server times for the step
        self.ft_seen = False            # the service sends session.step.first-token
        self.last_step_tok_s = None     # exact decode speed of the last finished step
        self.tools, self.args = {}, {}  # call id -> (since_ms, label)
        self.speed = dict.fromkeys(SPEED_KEYS, 0)
        self.ttft = []

    def _generated(self, created):
        self.last_gen_ms = created
        if self.first_gen_ms is None:
            self.first_gen_ms = created
        self.last_step_gen_ms = created

    def feed(self, ty, data, created):
        """Take one event of the session. Returns a label when a tool call started, else None."""
        ty = re.sub(r"\.\d+$", "", ty or "")    # durable types may carry a version suffix
        if ty in ("session.reasoning.delta", "session.text.delta"):
            n = len(data.get("delta") or "")
            if n:
                prev = self.last_gen_ms
                if prev is not None and created - prev <= 1000:
                    self.segs.append((prev, created, n))
                else:
                    self.segs.append((created, created, 0))   # new burst: its first delta has no duration
                self.last_delta_ms = created
                self.step_chars += n
                if ty == "session.reasoning.delta":
                    self.reason_chars += n
            self._generated(created)
        elif ty == "session.step.started":
            self.step_chars = self.reason_chars = 0
            try:
                self.step_start_ms = int(data["started"]) if data.get("started") is not None else created
            except (TypeError, ValueError):
                self.step_start_ms = created
            self.first_gen_ms = self.last_step_gen_ms = None
            self.first_tok_ms = self.streamed_ms = None
        elif ty == "session.step.first-token":
            self.ft_seen = True
            if self.first_tok_ms is None:
                self.first_tok_ms = created
        elif ty == "session.step.streamed":
            self.streamed_ms = created
        elif ty == "session.step.ended":
            self._step_ended(data.get("tokens") if isinstance(data.get("tokens"), dict) else {})
        elif ty == "session.tool.input.started":
            self._generated(created)
            if data.get("id") is not None:
                self.args[data["id"]] = (created, data.get("name") or data.get("tool") or "tool")
        elif ty == "session.tool.input.ended":
            self._generated(created)
            pending = self.args.pop(data.get("id"), None) if data.get("id") is not None else None
            if pending is not None:
                text = data.get("text")
                n = len(text if isinstance(text, str) else "" if text is None else str(text))
                self.step_chars += n
                if n:
                    self.segs.append((pending[0], max(pending[0], created), n))
        elif ty == "session.tool.called":
            inp = data.get("input") if isinstance(data.get("input"), dict) else {}
            detail = inp.get("command") or inp.get("path") or inp.get("filePath") or inp.get("pattern") or ""
            label = " ".join(f"{data.get('tool') or data.get('name') or 'tool'} {detail}".split())[:60] or "tool"
            self.tools[data.get("id") or f"call-{created}"] = (created, label)
            return label
        elif ty in ("session.tool.success", "session.tool.error", "session.tool.failed"):
            if data.get("id") is not None:
                self.tools.pop(data["id"], None)
        return None

    def _step_ended(self, tk):
        out_tok = int(tk.get("output") or 0) + int(tk.get("reasoning") or 0)
        # Hidden reasoning (Muse Spark: reasoning tokens billed, reasoning text empty) is generated
        # before the first streamed event, so such a step has no visible decode window; it only
        # counts for throughput.
        hidden = (int(tk.get("reasoning") or 0) > 0
                  and self.reason_chars < 0.5 * int(tk.get("reasoning") or 0))
        first, last, start = self.first_gen_ms, self.last_step_gen_ms, self.step_start_ms
        ft, streamed = self.first_tok_ms, self.streamed_ms
        if out_tok > 0 and ft is not None and streamed is not None:
            # OpenCode's own server times: counted exactly like its footer (a step whose window
            # is 0 still adds its tokens, as upstream does)
            win = max(0, streamed - ft)
            self.speed["ft_steps"] += 1
            self.speed["dec_tokens"] += out_tok
            self.speed["dec_ms"] += win
            if start is not None:
                self.speed["thr_tokens"] += out_tok
                self.speed["thr_ms"] += max(0, streamed - start)
                self.ttft.append(max(0, ft - start))
            if win >= 100:
                self.last_step_tok_s = int(round(out_tok / (win / 1000)))
        elif out_tok > 0 and last is not None and start is not None:
            self.speed["sse_steps"] += 1
            if last - start >= 100:
                self.speed["thr_tokens"] += out_tok
                self.speed["thr_ms"] += last - start
            if not hidden and first is not None and last - first >= 100:
                self.speed["dec_tokens"] += out_tok
                self.speed["dec_ms"] += last - first
                self.ttft.append(max(0, first - start))
        self.cal_chars += self.step_chars
        self.cal_tokens += out_tok
        self.done_out += out_tok
        self.step_chars = 0
        if self.cal_tokens > 0:
            self.cpt = min(8.0, max(1.5, self.cal_chars / self.cal_tokens))
        self.step_start_ms = self.first_gen_ms = self.last_step_gen_ms = None
        self.first_tok_ms = self.streamed_ms = None
        self.tools.clear()
        self.args.clear()

    def estimate(self):
        """Tokens of the step still streaming, estimated from its characters."""
        return int(round(self.step_chars / self.cpt))

    def view(self, now_ms):
        """(live tok/s, phase, tool_since, args_tool, args_since) for the status line. With
        OpenCode >= 2.3 the live number is the exact speed of the last finished step (none until
        one ends); older services get the character-based estimate of the last 5 s."""
        if not self.available:
            return None, None, None, None, None
        live_tok, self.segs = _live_rate(self.segs, now_ms, self.cpt)
        if self.ft_seen:
            live_tok = self.last_step_tok_s
        tool_since = args_since = args_tool = None
        if self.args:
            phase = "args"
            args_since = int(min(v[0] for v in self.args.values()) // 1000)
            args_tool = max(self.args.values(), key=lambda v: v[0])[1]
        elif self.tools:
            phase = "tool"
            tool_since = int(min(v[0] for v in self.tools.values()) // 1000)
        elif self.last_delta_ms is not None and now_ms - self.last_delta_ms <= 2000:
            phase = "writing"
        else:
            phase = "waiting"
        return live_tok, phase, tool_since, args_tool, args_since

    def ttft_s(self):
        med = _median(self.ttft)
        return round(med / 1000, 1) if med is not None else None


def watch_session(meter, get_sid, stop, on_label, tick):
    """Thread body: read the service's event stream, feed this session's events to the meter,
    and call tick() at most once a second."""
    resp = open_event_stream()
    if resp is None:
        return
    if stop.is_set():
        try:
            resp.close()
        except Exception:
            pass
        return
    with meter.lock:
        meter.resp = resp
        meter.available = True
    last_write = 0.0
    try:
        while not stop.is_set():
            try:
                line = resp.readline()
            except Exception:
                break
            if not line or stop.is_set():
                break
            s = get_sid()
            # Cheap pre-filter: most events belong to other sessions.
            if not s or s.encode() not in line:
                continue
            text = line.decode("utf-8", "replace")
            if not text.startswith("data:"):
                continue
            try:
                ev = json.loads(text[5:].strip())
            except ValueError:
                continue
            data = ev.get("data") or {}
            if data.get("sessionID") != s:
                continue
            try:
                created = int(ev["created"]) if ev.get("created") is not None else int(time.time() * 1000)
            except (TypeError, ValueError):
                created = int(time.time() * 1000)
            with meter.lock:
                label = meter.feed(ev.get("type"), data, created)
            if label:
                on_label(label)
            if stop.is_set():
                break
            now = time.time()
            if now - last_write >= 1:
                tick()
                last_write = now
    finally:
        with meter.lock:
            meter.available = False
            meter.resp = None
        try:
            resp.close()
        except Exception:
            pass


class Tally:
    """What `opencode run --format json` prints for one call: steps, tool calls, token totals,
    the final text and errors."""

    def __init__(self, session):
        self.sid = session
        self.steps = self.tools = self.n_events = 0
        self.tok = {"input": 0, "output": 0, "cache_read": 0}
        self.cost = 0.0
        self.texts, self.errors, self.error_raw = [], [], []
        self.last = "thinking"
        self._step_open = False
        self._msg, self._text = None, []

    def feed(self, ev):
        self.n_events += 1
        self.sid = ev.get("sessionID", self.sid)
        t, part = ev.get("type"), ev.get("part") or {}
        if t == "step_start":
            # A step_start with no step_finish since the last one is OpenCode retrying the same
            # step (e.g. provider overloaded).
            if not self._step_open:
                self.steps += 1
            self._step_open = True
            if self.tools == 0:
                self.last = "thinking"
        if t == "text":
            if part.get("messageID") != self._msg:
                self._msg, self._text = part.get("messageID"), []
                self.texts.append(self._text)
            self._text.append(part.get("text", ""))
        elif t in ("tool_use", "tool"):
            self.tools += 1
            nm = part.get("tool") or "tool"
            inp = (part.get("state") or {}).get("input") or {}
            detail = (inp.get("command") or "") if nm == "shell" else \
                (inp.get("path") or inp.get("filePath") or inp.get("pattern") or "")
            self.last = " ".join(f"{nm} {detail}".split())[:60]
        elif t == "step_finish":
            self._step_open = False
            tk = part.get("tokens") or {}
            self.tok["input"] += tk.get("input", 0)
            self.tok["output"] += tk.get("output", 0) + tk.get("reasoning", 0)
            self.tok["cache_read"] += (tk.get("cache") or {}).get("read", 0)
            self.cost += part.get("cost") or 0
        elif t == "error":
            err = ev.get("error") or {}
            self.errors.append(err.get("message") or (err.get("data") or {}).get("message")
                               or json.dumps(err)[:300] or "unknown error")
            self.error_raw.append(json.dumps(err)[:2000])

    def final_text(self):
        return "".join(self.texts[-1]).strip() if self.texts else ""


def opencode_cmd(msg, model, agent, session, files, fork):
    cmd = ["opencode", "run", "--format", "json", "--auto", "--agent", agent, "-m", model]
    if session:
        cmd += ["-s", session]
        if fork:
            # OpenCode Go pins a session id to one backend cell, so retrying the same id keeps
            # hitting the same overloaded one; a fork (new id, same history) can land elsewhere
            # (cheap-agent ticket 50, anomalyco/opencode#62306)
            cmd.append("--fork")
    for f in files:
        cmd += ["-f", f]
    return cmd + [msg]


def stop_worker(p, sid):
    """Stop a worker that is still running: interrupt the session in the service (killing the
    `opencode run` client alone leaves it running there), then the client. True if it had to."""
    if p.poll() is not None:
        return False
    if sid:
        try:
            interrupt_session(sid)
        except Exception:
            pass
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if p.poll() is not None:
            break
        try:
            os.killpg(p.pid, sig)
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    return True


def run_opencode(d, msg, model, agent, session=None, files=(), job=None,
                 limit_s=None, wrapup=False, deadline=None, fork=False):
    """One worker call: start `opencode run`, follow its output and the live event stream, keep the
    job's progress file fresh, stop it at the soft deadline or the hard limit (then one wrap-up
    call asks the same session for its report), and return the result dict."""
    c = cfg()
    limit_s = limit_s if limit_s is not None else c["timeout_s"]
    try:
        frac = float(c.get("soft_timeout_frac", 0.8))
    except (TypeError, ValueError):
        frac = 0.8
    soft_s = None if wrapup else limit_s * frac
    t0 = time.time()
    # totals carried over from earlier calls of the same job (fix rounds, retries)
    prev = (job or {}).get("_prog") or {}
    base = {"steps": int(prev.get("steps") or 0), "tools": int(prev.get("tools") or 0),
            "out": int(prev.get("out") or 0), "started": prev.get("started") or t0}
    if job is not None:
        job["_prog"] = dict(base)
    tally, meter = Tally(session), LiveMeter()

    def out_now():
        # stream-side exact count for finished steps + the estimate for the one still streaming;
        # stdout's step_finish events lag behind and would make the number jump
        return meter.done_out + meter.estimate() if meter.available else tally.tok["output"]

    def totals_now():
        return {"steps": base["steps"] + tally.steps, "tools": base["tools"] + tally.tools,
                "out": base["out"] + out_now(), "started": base["started"]}

    def write_progress(state, totals=None, **extra):
        if job is None:
            return
        with meter.lock:
            live_tok, phase, tool_since, args_tool, args_since = meter.view(int(time.time() * 1000))
            shown = (max(meter.tools.values(), key=lambda v: v[0])[1]
                     if meter.available and meter.tools else tally.last)
            totals = totals or totals_now()
            sp = set_speeds(dict(meter.speed))
            obj = {"state": state, "job": job["id"], "round": job["round"],
                   "model": model, "dir": os.path.basename(d),
                   "started": totals.get("started", base["started"]),
                   "updated": time.time(), "steps": totals["steps"], "tools": totals["tools"],
                   "last": shown, "pid": os.getpid(), "out_tokens": totals["out"],
                   # the part of out_tokens that is an estimate (the step still streaming);
                   # 0 once that step's exact count is in
                   "out_est": meter.estimate() if meter.available and state == "running" else 0,
                   "tok_s": sp["tok_s"], "tok_kind": sp["tok_kind"], "ttft_s": meter.ttft_s(),
                   "live_tok_s": live_tok, "live_kind": "step" if meter.ft_seen else "est", "phase": phase, "tool_since": tool_since,
                   "args_tool": args_tool, "args_since": args_since, "owner": job.get("owner") or ""}
            obj.update(extra)
            try:
                os.makedirs(PROGRESS, exist_ok=True)
                path = os.path.join(PROGRESS, job["id"] + ".json")
                tmp = path + f".tmp.{os.getpid()}"
                with open(tmp, "w") as f:
                    json.dump(obj, f)
                os.replace(tmp, path)
            except OSError:
                pass

    stderr_file = tempfile.NamedTemporaryFile(prefix="ds-opencode-", suffix=".log", delete=False)
    # opencode follows $PWD, not the process cwd
    p = subprocess.Popen(opencode_cmd(msg, model, agent, session, files, fork),
                         stdout=subprocess.PIPE, stderr=stderr_file, stdin=subprocess.DEVNULL,
                         text=True, cwd=d, env=dict(os.environ, PWD=d), start_new_session=True)
    q = queue.Queue()

    def _reader():
        try:
            for line in p.stdout:
                q.put(line)
        finally:
            q.put(None)

    threading.Thread(target=_reader, daemon=True).start()
    stop_live = threading.Event()

    def _set_last(label):
        tally.last = label

    live_thread = threading.Thread(target=watch_session, daemon=True, args=(
        meter, lambda: tally.sid, stop_live, _set_last, lambda: write_progress("running")))
    live_thread.start()
    timed_out = soft_hit = stopped_by_ds = False
    last_tick = 0.0
    try:
        while True:
            elapsed = time.time() - t0
            if elapsed > limit_s:
                timed_out = True
                break
            if soft_s is not None and soft_s < limit_s and elapsed > soft_s:
                soft_hit = True
                break
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                if time.time() - last_tick >= 1:
                    write_progress("running")
                    last_tick = time.time()
                continue
            if line is None:
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            tally.feed(ev)
            write_progress("running")
        if not timed_out and not soft_hit:
            p.wait()
    finally:
        stopped_by_ds = stop_worker(p, tally.sid)
        stop_live.set()
        with meter.lock:
            resp = meter.resp
        if resp is not None:
            try:
                resp.close()        # unblocks the watcher's readline
            except Exception:
                pass
        live_thread.join(timeout=2)
        with meter.lock:
            meter.available = False
        try:
            stderr_file.close()
        except Exception:
            pass
        stderr_text = stderr_tail(stderr_file.name)
        try:
            os.remove(stderr_file.name)
        except OSError:
            pass
    errors, sid = tally.errors, tally.sid
    if timed_out:
        errors.append(f"timeout after {int(limit_s)}s")
    if not timed_out and not soft_hit and not stopped_by_ds and p.returncode not in (None, 0):
        errors.append(f"opencode exited {p.returncode}: {stderr_text[:300] if stderr_text else 'no stderr'}")
    if tally.n_events == 0 and not errors:
        errors.append("no output from opencode: " + (stderr_text[:300] or "empty stderr"))
    final = tally.final_text()
    secs = round(time.time() - t0)
    write_progress("error" if errors else "done", updated=time.time(), secs=secs)
    if job is not None:
        job["_prog"] = totals_now()
    res = set_speeds({"session": sid, "report": final, "errors": errors, "tokens": tally.tok,
                      "cost": tally.cost, "tools": tally.tools, "secs": secs,
                      **meter.speed, "ttft": list(meter.ttft)})
    lim = limit_info(errors, tally.error_raw, model)
    if lim:
        res["limit"] = lim
    if soft_hit and not sid:
        res["errors"].append(f"soft deadline after {int(secs)}s (no session to wrap up)")
    elif soft_hit:
        wrap_limit = c["wrapup_s"]
        if deadline is not None:
            wrap_limit = min(wrap_limit, max(60, deadline - time.time()))
        w = run_opencode(d, WRAPUP_MSG, model, agent, session=sid, job=job,
                         limit_s=wrap_limit, wrapup=True, deadline=deadline)
        res["soft_deadline"] = {"at": secs, "limit_s": int(limit_s), "frac": frac}
        res["report"] = w["report"] or final
        for k in ("input", "output", "cache_read"):
            res["tokens"][k] += w["tokens"].get(k, 0)
        for k in ("cost", "secs", "tools", *SPEED_KEYS):
            res[k] += w.get(k) or 0
        res["ttft"] += w.get("ttft") or []
        set_speeds(res)
        res["errors"] += w["errors"]
        res["session"] = w.get("session") or sid
        ttft_med = _median(res["ttft"])
        write_progress("error" if res["errors"] else "done",
                       totals=job.get("_prog") if job is not None else None,
                       updated=time.time(), secs=res["secs"], tok_s=res["tok_s"],
                       tok_kind=res["tok_kind"],
                       ttft_s=round(ttft_med / 1000, 1) if ttft_med is not None else None)
    return res


OVERLOAD_RE = re.compile(r"overloaded|rate.?limit|too many requests|retry shortly"
                         r"|\b(429|503|529)\b", re.I)


# Provider-side failures worth switching models for: overload/rate limits that survived
# the retries, plus quota, billing, auth-on-model and 5xx errors (waiting won't fix those).
PROVIDER_FAIL_RE = re.compile(r"overloaded|rate.?limit|too many requests|retry shortly|quota|usage limit|spend limit"
                              r"|insufficient|exceeded|billing|credit|unavailable|bad gateway"
                              r"|\b(402|429|500|502|503|504|529)\b", re.I)


# OpenCode 2.x turns a plan limit into an error: "OpenCode Go <window> usage limit reached. …"
# (provider.go-limit), "Workspace usage limit reached", "You've reached the spend limit for this
# model", "Free usage limit reached", with retryAfterMs when the provider sent one.
LIMIT_RE = re.compile(r"usage limit reached|reached the spend limit|budget limit|limit resets", re.I)


def limit_info(errors, raw=(), model=None):
    """{model, message, resets} when an error is a plan/usage limit (not a blip), else None."""
    text = " ".join(str(e) for e in errors)
    if not LIMIT_RE.search(text + " ".join(raw)):
        return None
    msg = next((str(e) for e in errors if LIMIT_RE.search(str(e))), text)
    msg = re.split(r"\. (?:Switch|Connect|Or turn on|Check)", msg)[0].strip().rstrip(".")
    resets = None
    m = re.search(r'"retryAfterMs"\s*:\s*(\d+)', " ".join(raw))
    if m and int(m.group(1)) > 60_000:
        t = time.time() + int(m.group(1)) / 1000
        lt = time.localtime(t)
        resets = time.strftime("%H:%M" if lt.tm_yday == time.localtime().tm_yday else "%b %d %H:%M", lt)
    else:
        m = re.search(r"[Rr]esets? (?:in |at )?([^.]{2,40})", text)
        if m:
            resets = m.group(1).strip()
    return {"model": model, "message": msg[:200], "resets": resets}


def is_provider_failure(errors):
    return (any(PROVIDER_FAIL_RE.search(str(e)) for e in errors)
            and not any(str(e).startswith("timeout after") for e in errors))


FAILOVER_MSG = (
    "Your previous request failed on the model provider's side (not your fault); you are "
    "now running on a different model in the same session. Continue the task where it "
    "left off: check what is already done before redoing anything, then reply with the "
    "usual final report block.")


def is_overload(errors):
    # OpenCode also exits non-zero after giving up, which adds an "opencode
    # exited" error next to the provider's; timeouts never match.
    return (any(OVERLOAD_RE.search(str(e)) for e in errors)
            and not any(str(e).startswith("timeout after") for e in errors))


def merge_results(first, later):
    """Combine two run_opencode results of the same session; later wins for text."""
    out = dict(later)
    out["tokens"] = {k: first["tokens"].get(k, 0) + later["tokens"].get(k, 0)
                     for k in ("input", "output", "cache_read")}
    for k in ("cost", "secs", "tools", *SPEED_KEYS):
        out[k] = (first.get(k) or 0) + (later.get(k) or 0)
    out["ttft"] = (first.get("ttft") or []) + (later.get("ttft") or [])
    set_speeds(out)
    out["report"] = later.get("report") or first.get("report")
    out["session"] = later.get("session") or first.get("session")
    if first.get("soft_deadline") and not later.get("soft_deadline"):
        out["soft_deadline"] = first["soft_deadline"]
    return out


def run_retrying(d, msg, model, agent, session=None, files=(), job=None,
                 limit_s=None, deadline=None):
    """run_opencode, continuing the same session when the provider is overloaded.

    OpenCode already retries a step for about two minutes before giving up; this
    waits longer between whole attempts (config overload_wait_s) so a busy
    provider has time to recover instead of failing the job.
    """
    c = cfg()
    waits = list(c.get("overload_wait_s") or [])[:max(0, int(c.get("overload_retries") or 0))]
    t0 = time.time()
    res = run_opencode(d, msg, model, agent, session=session, files=files, job=job,
                       limit_s=limit_s, deadline=deadline)
    tries = 0
    while is_overload(res["errors"]) and tries < len(waits) and job is not None:
        sid = res.get("session") or session
        if not sid:
            break
        wait = float(waits[tries])
        left = [x for x in (
            (limit_s - (time.time() - t0)) if limit_s is not None else None,
            (deadline - time.time()) if deadline is not None else None) if x is not None]
        if left and min(left) < wait + FIX_MIN_S:
            break
        tries += 1
        write_progress_file(job, model, state="running",
                            last=f"provider overloaded · retry {tries}/{len(waits)}",
                            phase="tool", tool_since=int(time.time()), live_tok_s=None,
                            args_tool=None, args_since=None)
        time.sleep(wait)
        lim = None if limit_s is None else max(1.0, limit_s - (time.time() - t0))
        nxt = run_opencode(d, OVERLOAD_MSG, model, agent, session=sid, job=job,
                           limit_s=lim, deadline=deadline, fork=True)
        res = merge_results(res, nxt)
    if tries:
        res["overload_retries"] = tries
        if res["errors"]:
            res["errors"] = res["errors"] + [f"still failing after {tries} overload "
                                             "retr" + ("y" if tries == 1 else "ies")]
    fb = (c.get("fallback_model") or "").strip()
    if fb and fb == model:
        fb = other_model(model) or ""
    sid = res.get("session") or session
    if (fb and fb != model and job is not None and sid and res["errors"]
            and is_provider_failure(res["errors"])):
        left = [x for x in (
            (limit_s - (time.time() - t0)) if limit_s is not None else None,
            (deadline - time.time()) if deadline is not None else None) if x is not None]
        if not left or min(left) > FIX_MIN_S:
            first_err = str(res["errors"][0])[:160]
            write_progress_file(job, fb, state="running", last=f"failover to {fb}",
                                phase="tool", tool_since=int(time.time()), live_tok_s=None,
                                args_tool=None, args_since=None)
            lim = None if limit_s is None else max(1.0, limit_s - (time.time() - t0))
            nxt = run_opencode(d, FAILOVER_MSG, fb, agent, session=sid, job=job,
                               limit_s=lim, deadline=deadline)
            prev_errors = res["errors"]
            res = merge_results(res, nxt)
            res["errors"] = nxt["errors"]   # the fallback's outcome decides the job
            if nxt.get("limit"):
                res["limit"] = {**nxt["limit"], "also": res.get("limit")}
            else:
                res.pop("limit", None)
            res["failover"] = {"from": model, "to": fb, "reason": first_err,
                               "ok": not nxt["errors"], "first_errors": prev_errors[:2]}
            # later calls of this invocation (fix rounds, found note) stay on the fallback
            job.setdefault("model_primary", model)
            job["model"] = fb
    return res


def write_progress_file(job, model, **fields):
    path = os.path.join(PROGRESS, job["id"] + ".json")
    try:
        obj = json.load(open(path))
    except (OSError, ValueError):
        obj = {"state": "running", "started": time.time(), "steps": 0, "tools": 0,
               "out_tokens": 0, "tok_s": None, "ttft_s": None,
               "job": job["id"], "round": job["round"],
               "model": model, "dir": os.path.basename(proj(job))}
    obj["owner"] = job.get("owner") or ""
    obj.update(fields)
    obj["pid"] = os.getpid()
    obj["updated"] = time.time()
    try:
        os.makedirs(PROGRESS, exist_ok=True)
        tmp = path + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except OSError:
        pass


def write_check_progress(job, model, cmd):
    write_progress_file(job, model, state="running", last="check: " + cmd[:40],
                        phase="tool", tool_since=int(time.time()), live_tok_s=None,
                        args_tool=None, args_since=None)


def run_check(d, cmd, timeout_s):
    """Run cmd with bash -c in d; return pass flag, exit code and last 60 lines."""
    env = dict(os.environ, PWD=d)
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=d, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout_s)
        out, code = r.stdout or "", r.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        out += f"\n[check timed out after {timeout_s}s]"
        code = 124
    lines = out.splitlines()
    res = {"pass": code == 0, "code": code, "output": "\n".join(lines[-60:])}
    if code != 0:
        m = ENV_OUTPUT.search(out)
        if code in (126, 127) or m:
            res["env"] = f"exit {code}" + (f": {m.group(0).strip()[:120]}" if m else "")
    return res


# Output only a shell or loader prints when it cannot run the command at all (after cheap-agent's
# envFailure): no code edit fixes these, so fix rounds would only burn time.
ENV_OUTPUT = re.compile(r"command not found|No module named (?:pytest|pip)\b|is not recognized as an internal"
                        r"|cannot execute binary|No such file or directory: .*\b(node|npx|python3?|pytest|go|cargo)\b"
                        r"|ModuleNotFoundError: No module named '(?:pytest|_pytest)'", re.I)


# Failure condensing, ported from cheap-ag's errorTail (src/verify.ts, MIT).
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MARKER = re.compile(r"✗|✘|×|\bFAIL\b|Error:|error TS\d+|AssertionError|Traceback|panicked|^E\s{2,}")
_DETAIL = re.compile(r"expected|received|\bgot\b|\bwant\b|^\s*[-+] ", re.I)
_FRAME = re.compile(r'^\s*(at |File ")|\.(ts|tsx|js|mjs|cjs|py|go|rs):\d+')
_NOISE = re.compile(r"^\s*(✓|√|PASS\b|ok\b)|^\s*(Tests?|Test Files|Suites?)\b.*\bpassed\b"
                    r"|^\s*[.·•⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+$")


def error_tail(output, root=None, max_failures=3, max_lines=25):
    """Up to 3 distinct failures (repeats counted ×N), each with its message, expected/got
    lines and first in-repo frame; at most 25 lines of 200 chars. Falls back to the last
    lines when no failure marker is found."""
    text = _ANSI.sub("", output or "")
    if root:
        text = text.replace(root.rstrip("/") + "/", "")
    lines = [l.replace("\t", "  ").rstrip() for l in text.splitlines()]
    lines = [l for l in lines if l.strip() and (_MARKER.search(l) or not _NOISE.search(l))]
    fails = []
    i = 0
    while i < len(lines):
        if not _MARKER.search(lines[i]):
            i += 1
            continue
        block, frame, j = [lines[i].strip()], False, i + 1
        while j < len(lines) and j <= i + 12 and not _MARKER.search(lines[j]):
            if _DETAIL.search(lines[j]) and len(block) < 5:
                block.append("  " + lines[j].strip())
            elif (not frame and _FRAME.search(lines[j])
                  and not re.search(r"node_modules|node:internal|site-packages", lines[j])):
                block.append("  " + lines[j].strip())
                frame = True
            j += 1
        key = re.sub(r"\d+", "#", block[0])
        same = next((f for f in fails if f["key"] == key), None)
        if same:
            same["n"] += 1
        else:
            fails.append({"key": key, "lines": block, "n": 1})
        i = max(i + 1, j)
    if fails:
        body = []
        for f in fails[:max_failures]:
            head = f["lines"][0] + (f" (×{f['n']})" if f["n"] > 1 else "")
            body += [head] + f["lines"][1:]
    else:
        body = lines[-(max_lines - 1):]
    clip = lambda l: l if len(l) <= 200 else l[:120] + " … " + l[-77:]
    return [clip(l) for l in body[:max_lines]]


def same_failure(a, b):
    norm = lambda t: re.sub(r"\d+", "#", "\n".join(t))
    return norm(a) == norm(b)


FOUND_MSG = ("STOP. The check still fails and there will be no more fix rounds. Do not edit "
             "anything. Reply with ONLY one line starting `FOUND:` (at most 600 characters): "
             "where the problem is (file, function, line), your best hypothesis for the cause, "
             "and what you ruled out.")


def run_with_check(job, msg, session=None, files=(), budget_s=None):
    """Run the worker, then keep auto-fixing until the job's check passes."""
    d = job["dir"]
    c = cfg()
    try:
        budget_s = float(budget_s if budget_s is not None else c["job_budget_s"])
    except (TypeError, ValueError):
        budget_s = 1800.0
    deadline = time.time() + max(0.0, budget_s)

    def remaining():
        return deadline - time.time()

    def limit(cap):
        return min(cap, max(1.0, remaining()))

    res = run_retrying(d, msg, job["model"], job["agent"], session=session, files=files,
                       job=job, limit_s=limit(c["timeout_s"]), deadline=deadline)
    if res["errors"]:
        return res
    check = job.get("check")
    if not check:
        return res
    tries = max(0, int(job.get("fix_tries") or 0))
    timeout_s = c["check_timeout_s"]
    esc_model = (c.get("escalation_model") or "").strip()
    results, rounds, skipped, stuck, prev, escalated = [res], 0, 0, False, None, None
    while True:
        write_check_progress(job, job["model"], check)
        chk = run_check(d, check, limit(timeout_s))
        tail = error_tail(chk["output"], d)
        if chk["pass"] or chk.get("env") or escalated:
            break
        out_of_rounds = rounds >= tries
        if not out_of_rounds and prev is not None and same_failure(prev, tail):
            stuck = True   # the last fix round changed nothing about the failure
            out_of_rounds = True
        model = job["model"]
        if out_of_rounds:
            # cheap-agent's escalation: after the fix rounds, one round on a second model (a different model may get unstuck where the first is going in circles)
            # in the same session (config "escalation_model", off when empty)
            if esc_model == job["model"]:
                esc_model = other_model(job["model"]) or ""
            if not esc_model or remaining() < FIX_MIN_S:
                break
            model = escalated = esc_model
        prev = tail
        if remaining() < FIX_MIN_S:
            skipped = tries - rounds
            break
        if not escalated:
            rounds += 1
        fmsg = (f"AUTOMATED CHECK FAILED (not the reviewer). Command: {check}\n"
                f"Exit: {chk['code']}\nLast output lines:\n{chk['output']}\n"
                "Fix the cause within the original task scope, then reply with the "
                "final report block.")
        res = run_retrying(d, fmsg, model, job["agent"],
                           session=res.get("session") or job.get("session"), job=job,
                           limit_s=limit(c["timeout_s"]), deadline=deadline)
        results.append(res)
        if res["errors"]:
            return res
    last = results[-1]
    tok = {k: sum(r["tokens"].get(k, 0) for r in results)
           for k in ("input", "output", "cache_read")}
    out = dict(last)
    out["tokens"] = tok
    out["cost"] = sum(r.get("cost", 0) or 0 for r in results)
    out["secs"] = sum(r.get("secs", 0) or 0 for r in results)
    for k in SPEED_KEYS:
        out[k] = sum(r.get(k) or 0 for r in results)
    out["ttft"] = [x for r in results for x in (r.get("ttft") or [])]
    set_speeds(out)
    soft = [r["soft_deadline"] for r in results if r.get("soft_deadline")]
    if soft:
        out["soft_deadline"] = soft[0]
    retried = sum(r.get("overload_retries") or 0 for r in results)
    if retried:
        out["overload_retries"] = retried
    out["check"] = {"cmd": check, "pass": chk["pass"], "code": chk["code"],
                    "output": chk["output"], "tail": tail, "rounds": rounds, "stuck": stuck,
                    **({"escalated": escalated} if escalated else {}),
                    **({"env": chk["env"]} if chk.get("env") else {})}
    if not chk["pass"] and not chk.get("env") and not last["errors"] and remaining() > 60:
        fr = run_retrying(d, FOUND_MSG, job["model"], job["agent"],
                          session=last.get("session") or job.get("session"), job=job,
                          limit_s=limit(180), deadline=deadline)
        m = re.search(r"FOUND:\s*(.+)", fr.get("report") or "", re.S)
        if m:
            out["found"] = " ".join(m.group(1).split())[:600]
    if skipped:
        out["check"]["budget_skipped"] = skipped
    ttft_med = _median(out["ttft"])
    write_progress_file(job, job["model"], state="error" if last["errors"] else "done",
                        last="check: " + ("pass" if chk["pass"] else "fail"),
                        secs=out["secs"], out_tokens=tok["output"], tok_s=out["tok_s"],
                        tok_kind=out.get("tok_kind"), out_est=0,
                        ttft_s=(round(ttft_med / 1000, 1) if ttft_med is not None else None))
    return out


__all__ = [n for n in globals() if not n.startswith("__")]
