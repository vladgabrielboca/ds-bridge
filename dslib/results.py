"""ds-bridge: what a finished job becomes: status, result block, landing, undo, ledger, notifications."""
import hashlib, json, os, signal, subprocess, sys, time, argparse, shutil, queue, threading, fcntl
import fnmatch, re, tempfile, base64, collections, urllib.request, urllib.parse, secrets
import contextlib, io
from .core import *  # noqa: F401,F403
from .workspace import *  # noqa: F401,F403
from .runner import *  # noqa: F401,F403
LEDGER = os.path.join(STATE, "ledger.jsonl")
TODAY = os.path.join(STATE, "today.json")


def maybe_shadow_gc(gd):
    """Start a detached gc at most once per 24 h per shadow repo."""
    stamp = os.path.join(gd, "ds-gc-stamp")
    lock = stamp + ".lock"
    now = time.time()
    try:
        with open(lock, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                if now - os.path.getmtime(stamp) < 86400:
                    return
            except OSError:
                pass
            try:
                subprocess.Popen(["git", "-c", "core.quotePath=false",
                                  f"--git-dir={gd}", "gc", "--quiet",
                                  "--prune=1.hour.ago"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                return
            try:
                with open(stamp, "a"):
                    os.utime(stamp, (now, now))
            except OSError:
                pass
    except OSError:
        pass


def cleanup_shadow(job):
    """Drop the job's refs and index, then opportunistically gc the shadow repo."""
    gd, jid = job.get("shadow"), job["id"]
    if gd and os.path.isdir(gd):
        try:
            r = subprocess.run(["git", "-c", "core.quotePath=false",
                                f"--git-dir={gd}", "for-each-ref",
                                "--format=%(refname)", f"refs/ds/{jid}/"],
                               capture_output=True, text=True)
            for ref in r.stdout.splitlines():
                ref = ref.strip()
                if ref:
                    subprocess.run(["git", "-c", "core.quotePath=false",
                                    f"--git-dir={gd}", "update-ref", "-d", ref],
                                   capture_output=True)
        except OSError:
            pass
        maybe_shadow_gc(gd)
    try:
        os.remove(job_index(jid))
    except OSError:
        pass
TEST_RUNNERS = ["pytest", "python -m pytest", "python3 -m pytest", "npm test", "npm run test",
                "node --test", "npx vitest", "npx jest", "go test", "cargo test", "bun test",
                "make test", "~/Projects/videos/.venv/bin/python -m pytest"]
TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[^/]+$"
                       r"|\.(test|spec)\.[^/]+$")


def verify_repro(job, report):
    """Run the worker's REPRO command on the new code (must pass) and on a copy where every
    non-test change is reverted (must fail). Returns a one-line verdict."""
    m = re.search(r"^\s*REPRO:\s*(.+)$", report or "", re.M)
    if m:
        job["repro_cmd"] = m.group(1).strip().strip("`").strip()
    cmd = job.get("repro_cmd")
    if not cmd:
        return "unverified: the worker reported no REPRO line"
    # The command comes from the worker: run it only when it starts with one of the job's own
    # check commands or a known test runner, and has no shell control characters (as cheap-agent).
    allowed = [seg.strip() for seg in re.split(r"&&|\|\||;|\|", job.get("check") or "") if seg.strip()]
    allowed += TEST_RUNNERS
    if re.search(r"[;&|`$<>]", cmd) or not any(cmd == a or cmd.startswith(a + " ") for a in allowed):
        return f"unverified: `{cmd[:120]}` is not one of the job's check commands or a test runner"
    if cmd.lower().startswith("none"):
        return "none: " + cmd.split(":", 1)[-1].strip()[:200]
    c = cfg()
    w, gd = job["dir"], job["shadow"]
    post = job.get("post") or job["base"]
    new = run_check(w, cmd, c["check_timeout_s"])
    if not new["pass"]:
        return f"FAILED: `{cmd}` does not pass on the new code (exit {new['code']})"
    names = [f for f in git(gd, w, "diff", "--name-only", "-z", "--no-renames",
                            job["base"], post).split("\0") if f]
    tests = [f for f in names if TEST_PATH.search(f)]
    if not tests:
        return f"unverified: `{cmd}` passes, but the diff has no test file"
    old, err = reflink_copy(w, job["id"] + "-repro")
    if not old:
        return f"unverified: `{cmd}` passes; no copy for the old-code run ({err})"
    try:
        for f in names:
            if f in tests:
                continue
            dst = os.path.join(old, f)
            if commit_blob(gd, w, job["base"], f) == "absent":
                try:
                    os.remove(dst)
                except OSError:
                    pass
            else:
                blob = subprocess.run(["git", f"--git-dir={gd}", "show", f"{job['base']}:{f}"],
                                      capture_output=True)
                os.makedirs(os.path.dirname(dst) or old, exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(blob.stdout)
        before = run_check(old, to_copy(cmd, w, old), c["check_timeout_s"])
    finally:
        shutil.rmtree(old, ignore_errors=True)
    if before["pass"]:
        return f"WEAK: `{cmd}` also passes on the old code, so it does not reproduce the bug"
    return f"fail→pass verified: `{cmd}`"


def diff_text(job, full=False, paths=(), since=None):
    d, gd = job["dir"], job["shadow"]
    idx = job_index(job["id"])
    job_paths = job.get("paths")
    stage_paths(gd, d, job_paths, idx)
    scope = list(paths) if paths else (job_paths or [])
    since = since or job["base"]
    stat = git(gd, d, "diff", "--cached", "--stat=100", since, "--", *scope, index=idx)
    body = git(gd, d, "diff", "--cached", "-U2", "--no-color", since, "--", *scope, index=idx)
    lines = body.splitlines()
    jdir = os.path.join(JOBS, job["id"])
    open(os.path.join(jdir, "full.diff"), "w").write(body)
    cap = cfg()["max_diff_lines"]
    if not full and len(lines) > cap:
        body = "\n".join(lines[:cap]) + (
            f"\n... [diff truncated: {len(lines) - cap} more lines. "
            f"`ds diff {job['id']} --full` or `ds diff {job['id']} <path>`; "
            f"file: {jdir}/full.diff]")
    return stat.strip(), body


def save(job):
    p = os.path.join(JOBS, job["id"], "job.json")
    tmp = p + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump({k: v for k, v in job.items() if not k.startswith("_")},
                  f, indent=1)
    os.replace(tmp, p)


def load(jid):
    p = os.path.join(JOBS, jid, "job.json")
    if not os.path.exists(p):
        die(f"no job {jid}")
    return json.load(open(p))


def refresh_today():
    """Recompute today.json (jobs run today) from the ledger."""
    today = time.strftime("%Y-%m-%d")
    job_ids = set()
    try:
        with open(LEDGER) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts = e.get("ts") or 0
                try:
                    day = time.strftime("%Y-%m-%d", time.localtime(ts))
                except (OSError, ValueError, OverflowError):
                    continue
                if day != today:
                    continue
                if e.get("job"):
                    job_ids.add(e["job"])
    except OSError:
        pass
    try:
        tmp = TODAY + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"date": today, "jobs": len(job_ids)}, f)
        os.replace(tmp, TODAY)
    except OSError:
        pass


def record_ledger(job, res, claude_chars):
    tk = res["tokens"]
    ttft_med = _median(res.get("ttft") or [])
    entry = {"ts": int(time.time()), "job": job["id"], "round": job["round"],
             "dir": proj(job), "worker_in": tk["input"],
             "worker_cache": tk["cache_read"], "worker_out": tk["output"],
             "claude_chars": claude_chars, "secs": res.get("secs"),
             "tok_s": res.get("tok_s"), "tok_kind": res.get("tok_kind"), "tok_src": res.get("tok_src"),
             "dec_tok_s": res.get("dec_tok_s"), "thr_tok_s": res.get("thr_tok_s"),
             "ttft_ms": (int(round(ttft_med)) if ttft_med is not None else None),
             "check": (None if not res.get("check")
                       else "pass" if res["check"]["pass"] else "fail")}
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, ".ledger.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(LEDGER, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            refresh_today()
    except Exception:
        pass


REPORT_KEYS = ("STATUS", "SUMMARY", "RISKS", "REVIEW", "MISSING", "DISAGREE", "VISUAL",
               "QUESTION", "FOUND", "REPRO", "CHECKS", "NOTES")
NONE_RE = re.compile(r"^\s*(none|n/?a|-)?\s*\.?\s*$", re.I)


def parse_report(rep):
    """The worker's final block as {KEY: text}; continuation lines join their key."""
    out, cur = {}, None
    for line in (rep or "").splitlines():
        m = re.match(r"^\s*([A-Z]+):\s*(.*)$", line)
        if m and m.group(1) in REPORT_KEYS:
            cur = m.group(1)
            out[cur] = m.group(2).strip()
        elif cur and line.strip():
            out[cur] = (out[cur] + " " + line.strip().lstrip("- ")).strip()
    return {k: v for k, v in out.items() if not NONE_RE.match(v)}


def numstat(job, since=None):
    gd, w = job["shadow"], job["dir"]
    base = since or job["base"]
    post = job.get("post") or base
    out = git(gd, w, "diff", "--numstat", "--no-renames", base, post, check=False)
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            a, d_, p = parts
            files.append((p, int(a) if a.isdigit() else 0, int(d_) if d_.isdigit() else 0))
    return files


def job_status(res, rep):
    chk = res.get("check")
    if res["errors"]:
        if res.get("limit"):
            return "LIMIT"
        return "PROVIDER" if is_provider_failure(res["errors"]) else "ERROR"
    if rep.get("STATUS", "").upper().startswith("BLOCKED"):
        return "BLOCKED"
    if chk and not chk.get("pass"):
        return "FAIL"
    # a weak test never throws away a passing fix (cheap-agent ticket 52): it lands with a repro: risk line
    if (res.get("repro") or "").startswith("FAILED"):
        return "FAIL"
    if res.get("soft_deadline"):
        # the wrap-up asks the worker whether anything is unfinished: DONE, nothing MISSING and a
        # passing check is finished work that merely ran long
        done = (rep.get("STATUS", "").upper().startswith("DONE") and not rep.get("MISSING")
                and chk and chk.get("pass"))
        if not done:
            return "PARTIAL"
    return "OK"


REDO_MSG = ("Your change could not land: the project changed meanwhile in {files}. Your copy was rebuilt "
            "from the current project with your change applied again where it could be; files where both "
            "changed the same lines hold <<<<<<< ======= >>>>>>> conflict markers: {marked}. Keep the "
            "project's current changes, redo your task's intent on top of them, leave no conflict markers, "
            "run the check, then reply with the final report block.")


def auto_land(job, res):
    """Land a passed job. Copy jobs: 3-way apply, one automatic redo on conflict. Returns None or the
    conflict text; on success the job is "landed" (undo with ds reject until it is cleaned up)."""
    if job.get("project"):
        conflict = land(job)
        if conflict:
            names = conflict_paths(conflict) or ["(see ds diff)"]
            marked = rebase_job(job)
            rr = run_with_check(job, REDO_MSG.format(files=", ".join(names), marked=", ".join(marked) or "none"),
                                session=job.get("session"))
            job["post"] = snapshot(job["dir"], job["shadow"], f"post redo {job['id']}",
                                   job_index(job["id"]), job["id"], job.get("paths"))
            res["redo"] = True
            if rr.get("report"):
                res["report"] = rr["report"]
            ok = (not rr["errors"] and (not rr.get("check") or rr["check"].get("pass"))
                  and not leftover_markers(job))
            if not ok:
                save(job)
                return conflict
            conflict = land(job)
            if conflict:
                save(job)
                return conflict
        drop_copy(job)
    job["status"] = "landed"
    job["landed_at"] = time.time()
    save(job)
    return None


def conflict_paths(text):
    named = re.findall(r"^Applied patch to '(.+)' with conflicts\.$|^error: (.+?): (?:does not exist in index"
                       r"|does not match index|already exists in (?:index|working directory)|patch does not apply)$",
                       text or "", re.M)
    return sorted(set(a or b for a, b in named))


DISCIPLINE = os.path.join(HOME, ".local/state/ds-bridge/discipline")


def record_outcome(state):
    """Tell the discipline hook how this job or batch ended, for the chat that started it: the
    hook cannot see a result that reaches Claude as a background notice. state: ok | notdone | None."""
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid or state not in ("ok", "notdone"):
        return
    try:
        os.makedirs(DISCIPLINE, exist_ok=True)
        path = os.path.join(DISCIPLINE, re.sub(r"[^\w-]", "_", sid) + ".json.result")
        tmp = path + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"state": state, "ts": time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def outcome_of(status, chk):
    """The same reading the hook makes of a result block's first line."""
    if status in ("PARTIAL", "CONFLICT", "BLOCKED") or (status == "FAIL" and not (chk or {}).get("env")):
        return "notdone"
    if status == "OK" and chk and chk.get("pass"):
        return "ok"
    return None


def finish_job(job, res):
    """Decide the status, auto-land a passed job, print the compact result block."""
    c = cfg()
    raw = res["report"] or ""
    rep = parse_report(raw)
    status = job_status(res, rep)
    chk = res.get("check")
    landed, conflict = False, None
    if (status == "OK" and chk and chk.get("pass") and c.get("auto_land", True)
            and not res.get("outside")):
        conflict = auto_land(job, res)
        landed = conflict is None
        if conflict:
            status = "CONFLICT"
    text = render_block(job, res, rep, raw, status, landed, conflict)
    try:
        with open(os.path.join(JOBS, job["id"], "last.txt"), "w") as f:
            f.write(text)
    except OSError:
        pass
    sys.stdout.write(text)
    record_ledger(job, res, len(text))
    if not os.environ.get("DS_BATCH"):      # a batch reports once, for all its jobs
        record_outcome(outcome_of(status, chk))
    return status


def render_block(job, res, rep, raw, status, landed, conflict):
    c = cfg()
    jid = job["id"]
    chk = res.get("check")
    rb = job.get("round_base")
    files = numstat(job, since=rb)
    add, dele = sum(f[1] for f in files), sum(f[2] for f in files)
    head = [status, jid, f"files={len(files)}", f"+{add}/-{dele}"]
    if chk:
        head.append("check=" + ("pass" if chk.get("pass") else "env" if chk.get("env") else "fail"))
        if chk.get("escalated"):
            head.append("esc=" + chk["escalated"].split("/")[-1])
    repro = res.get("repro") or ""
    if repro:
        head.append("repro=" + ("fail→pass" if repro.startswith("fail→pass") else repro.split(":")[0].split()[0].lower()))
    if job.get("model") and job["model"] != c["model"]:
        head.append("model=" + job["model"].split("/")[-1])
    head.append(f"{res.get('secs', 0)}s")
    if job["round"] > 1:
        head.append(f"round={job['round']}")
    if res.get("redo"):
        head.append("redo=1")
    if landed:
        head.append("landed")
    elif job.get("project"):
        head.append("in-copy")
    out = [" ".join(head)]
    summary = rep.get("SUMMARY") or (raw.strip().splitlines()[0] if raw.strip() else "(worker gave no report)")
    out.append("summary: " + _clip(summary, 200))
    if files and status != "BLOCKED":
        out.append("changed: " + ", ".join(f"{p} +{a}/-{d}" for p, a, d in files[:15])
                   + (f", … +{len(files) - 15} more" if len(files) > 15 else ""))
    for key in ("RISKS", "REVIEW", "MISSING", "DISAGREE", "VISUAL"):
        if rep.get(key):
            out.append(f"{key.lower()}: {_clip(rep[key], 400)}")
    if status == "BLOCKED" and rep.get("QUESTION"):
        out.append("question: " + _clip(rep["QUESTION"], 600))
    if repro and not repro.startswith("fail→pass"):
        out.append("repro: " + _clip(repro, 300))
    if status not in ("OK",):
        found = res.get("found") or rep.get("FOUND")
        if found:
            out.append("found: " + _clip(found, 600))
    if chk and not chk.get("pass"):
        if chk.get("env"):
            out.append(f"error: environment: `{_clip(chk['cmd'], 120)}` could not run ({chk['env']}); "
                       "no code fix was attempted")
        else:
            out.append(f"error: `{_clip(chk['cmd'], 160)}` exit {chk['code']} after {chk['rounds']} fix rounds"
                       + ("; the same failure repeated" if chk.get("stuck") else ""))
            for line in (chk.get("tail") or chk["output"].splitlines()[-15:])[:15]:
                out.append("  " + line)
    lim = res.get("limit")
    if lim:
        out.append("limit: " + lim["message"] + (f" (resets {lim['resets']})" if lim.get("resets") else
                                                 " (reset time not given)")
                   + (f"; {lim['also']['message']} too" if lim.get("also") else ""))
    elif res["errors"]:
        out.append("error: " + _clip(" | ".join(e[:300] for e in res["errors"]), 500))
    fo = res.get("failover")
    if fo:
        out.append(f"note: {fo['from']} failed on the provider side; continued on {fo['to']}"
                   + ("" if fo["ok"] else " (it failed too)"))
    if res.get("soft_deadline"):
        out.append(f"note: soft deadline hit at {res['soft_deadline']['at']}s; the worker wrapped up early")
    if job.get("copy_error"):
        out.append(f"note: no private copy ({job['copy_error']}); ran in place")
    if conflict:
        out.append("conflict: " + ", ".join(conflict_paths(conflict) or ["see ds diff"])
                   + " (the automatic redo could not reconcile it; the project is unchanged)")
    if res.get("outside"):
        out.append("warning: changes outside --paths: " + "; ".join(res["outside"][:10]))
    if res.get("excluded_note"):
        out.append(res["excluded_note"].replace("--- NOTE ", "note: "))
    if job.get("git_note") and job.get("round", 1) == 1:
        out.append(job["git_note"])
    block = "\n".join(out)
    if len(block) > c.get("max_block_chars", 1600):
        block = block[:c.get("max_block_chars", 1600)] + " …"
    trusted = (chk and chk.get("pass") and c.get("diff_on_pass", "stat") == "stat"
               and status in ("OK", "CONFLICT") and not res.get("outside"))
    tail = []
    if status not in ("BLOCKED", "PROVIDER", "ERROR", "LIMIT") and not trusted:
        stat, body = diff_text(job, since=rb)
        if body:
            tail += ["--- diff" + (f" (this round; cumulative: ds diff {jid})" if rb else ""), body]
    if status == "OK" and landed:
        tail.append(f"next: nothing to do (undo: ds reject {jid})")
    elif status == "OK":
        tail.append(f"next: ds approve {jid} | ds revise {jid} \"1. ...\" | ds reject {jid}")
    elif status == "BLOCKED":
        tail.append(f"next: ds reply {jid} <option letter or short answer>")
    elif status == "CONFLICT":
        tail.append(f"next: ds revise {jid} \"1. In <file>, resolve ...\" | ds reject {jid}")
    elif status == "LIMIT":
        tail.append("next: usage limit: tell the user which limit and when it resets; don't retry, "
                    "don't do the work yourself.")
    elif status in ("PROVIDER", "ERROR"):
        tail.append("next: ds/provider error: tell the user and stop; don't do the work yourself.")
    elif c.get("takeover") == "sonnet":
        tail.append(f"next: not finished: hand it to the Agent tool with model sonnet, working in {job['dir']} "
                    "(give it the task, the user's words, found:, error: and the check command; it fixes "
                    f"and runs the check there), then ds approve {jid}. Report its answer; don't do it yourself.")
    else:
        tail.append(f"next: not finished: ask the user one question: ds revise {jid} with the findings, "
                    "you finish it yourself, or stop. Don't pick for them; don't take it over yourself.")
    return block + "\n" + "\n".join(tail) + "\n"


def gc_landed(max_age_s=86400):
    """Landed jobs older than a day become approved and drop their snapshots (no more undo)."""
    try:
        names = os.listdir(JOBS)
    except OSError:
        return
    now = time.time()
    for n in names:
        p = os.path.join(JOBS, n, "job.json")
        try:
            job = json.load(open(p))
        except (OSError, ValueError):
            continue
        if job.get("status") == "landed" and now - float(job.get("landed_at") or 0) > max_age_s:
            job["status"] = "approved"
            save(job)
            cleanup_shadow(job)


def notify_cmd(job, res):
    """Return the notify-send argv for a finished job, or None to stay quiet."""
    c = cfg()
    if not c.get("notify", True):
        return None
    try:
        min_s = int(c.get("notify_min_s", 15))
    except (TypeError, ValueError):
        min_s = 15
    secs = int(res.get("secs") or 0)
    lim = res.get("limit")
    if lim and shutil.which("notify-send"):
        # a plan limit is always worth a notification, however fast it came back
        return ["notify-send", "-a", "ds-bridge", "-u", "critical", f"{short_model(job.get('model') or '').capitalize() or 'ds'}: usage limit reached",
                lim["message"] + (f" · resets {lim['resets']}" if lim.get("resets") else "")]
    if secs < min_s or not shutil.which("notify-send"):
        return None
    base = os.path.basename(proj(job).rstrip("/")) or proj(job)
    when = f"{secs // 60}:{secs % 60:02d}"
    errs = res.get("errors") or []
    if errs:
        title = f"{short_model(job.get('model') or '').capitalize() or 'ds'} {job['id']} failed"
        body = f"{base} · {when} · {str(errs[0])[:100]}"
    else:
        title = f"{short_model(job.get('model') or '').capitalize() or 'ds'} {job['id']} done"
        body = f"{base} · {when}"
        chk = res.get("check")
        if chk:
            body += " · check " + ("PASS" if chk["pass"] else "FAIL")
    return ["notify-send", "-a", "ds-bridge", title, body]


def notify_done(job, res):
    try:
        cmd = notify_cmd(job, res)
        if cmd:
            subprocess.run(cmd, timeout=5, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _apply_3way(gd, wt, patch, files, index):
    """3-way apply patch onto wt through a throwaway index synced to wt's current files.
    Returns (ok, output). The user's own git index is never touched."""
    try:
        os.remove(index)
    except OSError:
        pass
    present = [f for f in files if os.path.lexists(os.path.join(wt, f))]
    if present:
        git(gd, wt, "add", "-A", "-f", "--", *present, index=index)
    env = dict(os.environ, GIT_INDEX_FILE=index)
    base = ["git", "-c", "core.quotePath=false", f"--git-dir={gd}", f"--work-tree={wt}"]
    chk = subprocess.run(base + ["apply", "--3way", "--check", "-"], input=patch,
                         capture_output=True, text=True, cwd=wt, env=env)
    out = (chk.stdout or "") + (chk.stderr or "")
    # `apply --3way --check` exits 0 even when it would leave conflict markers
    if chk.returncode or "with conflicts" in out:
        return False, out.strip() or "patch does not apply"
    r = subprocess.run(base + ["apply", "--3way", "-"], input=patch,
                       capture_output=True, text=True, cwd=wt, env=env)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()


def _reverse(gd, wt, job):
    post = job.get("post") or job["base"]
    return git(gd, wt, "diff", "--binary", "--no-renames", post, job["base"])


def _job_patch(job):
    gd, w = job["shadow"], job["dir"]
    post = job.get("post") or job["base"]
    names = [f for f in git(gd, w, "diff", "--name-only", "-z", "--no-renames",
                            job["base"], post).split("\0") if f]
    patch = git(gd, w, "diff", "--binary", "--no-renames", job["base"], post) if names else ""
    return patch, names


def copy_back_untracked(job):
    """Files the job created or changed in its copy that snapshots skip (renders, media,
    big files): reflink them into the project. Returns (copied, skipped)."""
    w, d = job["dir"], job["project"]
    post = job.get("post") or job["base"]
    tracked = set(f for f in git(job["shadow"], w, "ls-tree", "-r", "-z", "--name-only",
                                 post).split("\0") if f)
    t0 = float(job.get("started") or 0)
    copied, skipped = 0, []
    skip_dirs = EXCLUDED_PRUNE | link_skip()
    try:
        big = float(cfg().get("big_file_mb", 5)) * 1024 * 1024
    except (TypeError, ValueError):
        big = 5 * 1024 * 1024
    cands = []
    for root, dirs, files in os.walk(w):
        dirs[:] = [x for x in dirs if x not in skip_dirs]
        for name in files:
            src = os.path.join(root, name)
            rel = os.path.relpath(src, w)
            try:
                st = os.lstat(src)
                if rel in tracked or st.st_mtime <= t0:
                    continue
            except OSError:
                continue
            cands.append((src, rel, st.st_size))
    # Only files a snapshot leaves out on purpose travel as copies: git-ignored output, excluded
    # media types, big files. A plain file outside the job's --paths is NOT one of those: copying
    # it would land an unreviewed, unmerged change that reject cannot undo (it is listed instead).
    ignored = git_ignored(w, [rel for _, rel, _ in cands])
    left_out = []
    for src, rel, size in cands:
        if not (rel in ignored or excluded_name(rel) or size > big):
            left_out.append(rel)
            continue
        dst = os.path.join(d, rel)
        try:
            if os.path.lexists(dst) and os.lstat(dst).st_mtime > t0:
                skipped.append(rel)   # changed in the project meanwhile: keep that
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            subprocess.run(["cp", "-a", "--reflink=auto", src, dst], check=True,
                           capture_output=True)
            copied += 1
        except (OSError, subprocess.CalledProcessError):
            skipped.append(rel)
    job["left_out"] = sorted(left_out)
    return copied, skipped


def leftover_markers(job):
    """Files whose job changes still add conflict marker lines."""
    post = job.get("post") or job["base"]
    out = git(job["shadow"], job["dir"], "diff", "--check", job["base"], post, check=False)
    return sorted(set(re.findall(r"^(.+?):\d+: leftover conflict marker$", out, re.M)))


def land(job):
    """Apply a copied job's changes to the project. Returns None or the conflict text."""
    d, gd = job["project"], job["shadow"]
    patch, names = _job_patch(job)
    with open(gd + ".land.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if patch.strip():
            ok, out = _apply_3way(gd, d, patch, names,
                                  os.path.join(JOBS, job["id"], "land.index"))
            if not ok:
                return out
        job["copied_back"] = copy_back_untracked(job)
    return None


def rebase_job(job):
    """Fresh copy of the current project with the job's changes 3-way applied; returns the files left
    with conflict markers. The job now points at the new copy and base."""
    d, gd, jid = job["project"], job["shadow"], job["id"]
    patch, names = _job_patch(job)
    job["rebases"] = int(job.get("rebases") or 0) + 1
    w2, err = make_copy(d, f"{jid}-r{job['rebases']}")
    if not w2:
        die(f"could not make a fresh copy: {err}")
    old = job["dir"]
    base2 = snapshot(w2, gd, f"rebase {jid}", job_index(jid), jid, job.get("paths"))
    idx = os.path.join(JOBS, jid, "rebase.index")
    try:
        os.remove(idx)
    except OSError:
        pass
    present = [f for f in names if os.path.lexists(os.path.join(w2, f))]
    if present:
        git(gd, w2, "add", "-A", "-f", "--", *present, index=idx)
    r = subprocess.run(["git", "-c", "core.quotePath=false", f"--git-dir={gd}",
                        f"--work-tree={w2}", "apply", "--3way", "-"], input=patch,
                       capture_output=True, text=True, cwd=w2,
                       env=dict(os.environ, GIT_INDEX_FILE=idx))
    unmerged = git(gd, w2, "ls-files", "-u", "-z", index=idx, check=False).split("\0")
    conflicted = sorted(set(e.split("\t", 1)[1] for e in unmerged if "\t" in e))
    job.update(dir=w2, base=base2, round_base=None, started=time.time())
    job["post"] = snapshot(w2, gd, f"post rebase {jid}", job_index(jid), jid, job.get("paths"))
    save(job)
    remove_worktree(worktree_top(old), job.get("repo") or repo_root(d))
    conflicted = sorted(set(conflicted) | set(leftover_markers(job)))
    job["_rebase_err"] = (r.stderr or "").strip()[:300] if r.returncode and not conflicted else ""
    return conflicted


def commit_blob(gd, wt, commit, path):
    """Blob hash of path in commit, or "absent"."""
    out = git(gd, wt, "ls-tree", "-z", commit, "--", path).strip("\0\n")
    if not out:
        return "absent"
    parts = out.split("\t", 1)[0].split()
    if len(parts) >= 3 and parts[1] in ("blob", "commit"):
        return parts[2]
    return "absent"


def worktree_blob(gd, wt, path):
    """Blob hash of the current working file, or "absent"/"unreadable"."""
    if not os.path.lexists(os.path.join(wt, path)):
        return "absent"
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false",
                            f"--git-dir={gd}", f"--work-tree={wt}",
                            "hash-object", "--", path],
                           capture_output=True, text=True, cwd=wt)
    except OSError:
        return "unreadable"
    if r.returncode:
        return "unreadable"
    return r.stdout.strip()


def prune_empty_dirs(root, rel):
    """Remove parent dirs of rel left empty, never reaching root itself."""
    root = os.path.realpath(root)
    parent = os.path.dirname(rel.replace(os.sep, "/"))
    while parent and parent not in (".", ""):
        path = os.path.realpath(os.path.join(root, parent))
        if path == root or not path.startswith(root + os.sep):
            break
        try:
            os.rmdir(path)
        except OSError:
            break
        parent = os.path.dirname(parent)


__all__ = [n for n in globals() if not n.startswith("__")]
