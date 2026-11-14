"""ds-bridge: read-only and chat commands: list, status, stats, cost, log, delegate, playbook."""
import hashlib, json, os, signal, subprocess, sys, time, argparse, shutil, queue, threading, fcntl
import fnmatch, re, tempfile, base64, collections, urllib.request, urllib.parse, secrets
import contextlib, io
from .core import *  # noqa: F401,F403
from .workspace import *  # noqa: F401,F403
from .runner import *  # noqa: F401,F403
from .results import *  # noqa: F401,F403
from .jobs import *  # noqa: F401,F403
from .batch import *  # noqa: F401,F403
DELEGATE_TTL = 30 * 24 * 60 * 60


def cmd_stats(a):
    now = time.time()
    if a.all:
        cutoff = None
    elif a.days is not None:
        cutoff = now - int(a.days) * 86400
    else:
        lt = time.localtime()
        cutoff = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    rows = []
    try:
        with open(LEDGER) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if cutoff is None or e.get("ts", 0) >= cutoff:
                    rows.append(e)
    except OSError:
        pass
    win = sum(int(e.get("worker_in") or 0) for e in rows)
    wca = sum(int(e.get("worker_cache") or 0) for e in rows)
    wout = sum(int(e.get("worker_out") or 0) for e in rows)
    chars = sum(int(e.get("claude_chars") or 0) for e in rows)
    jobs = len({e.get("job") for e in rows if e.get("job")})
    print(f"jobs {jobs} · rounds {len(rows)} · worker time {fmt_secs(sum(int(e.get('secs') or 0) for e in rows))}")
    print(f"worker tokens (provider-reported): in {fmt_num(win)} / cache {fmt_num(wca)} / out {fmt_num(wout)}")
    print(f"ds output read by Claude: {fmt_num(chars)} chars")
    # rows written before 2026-11-13 hold a decode figure that was wrong for hidden-reasoning models
    new = [e for e in rows if "dec_tok_s" in e]
    thr = [e["thr_tok_s"] for e in new if e.get("thr_tok_s") is not None]
    # OpenCode >= 2.3 rows: decode from the server's first-token time (same formula as its footer)
    oc = [e["dec_tok_s"] for e in new if e.get("tok_src") == "opencode" and e.get("dec_tok_s") is not None]
    dec = [e["dec_tok_s"] for e in new if e.get("tok_src") != "opencode" and e.get("dec_tok_s") is not None]
    ttfts = [e["ttft_ms"] for e in new if e.get("ttft_ms") is not None]
    parts = []
    if thr:
        parts.append(f"throughput median {int(round(_median(thr)))} tok/s (incl. first-token wait)")
    if oc:
        parts.append(f"decode median {int(round(_median(oc)))} tok/s (OpenCode first-token, {len(oc)} rounds)")
    if dec:
        parts.append(f"decode median {int(round(_median(dec)))} tok/s (older SSE estimate, streamed steps)")
    if ttfts:
        parts.append(f"first token median {_median(ttfts) / 1000:.1f}s")
    if parts:
        print(f"speed: {' · '.join(parts)} ({len(new)} rounds)")
    print("What a chat cost Claude is measured from its transcript: ds cost [SESSION_ID]")


COST_W = {"in": 1.0, "cache_write": 1.25, "cache_read": 0.1, "out": 5.0}
# Characters per token of a file Claude writes, measured on 141 Write calls in real transcripts
# (context growth after the call; quartiles 1.75 / 2.02 / 2.22). The high quartile is used so the
# "Claude types it" floor comes out low rather than high.
CHARS_PER_TOKEN = 2.2
DS_CMD_RE = re.compile(r"(?:^|[\s;&|/])ds\s+(?!cost\b|stats\b)\w")


def _iso_ts(s):
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _snip(t, n=70):
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n] + "…"


def _use_label(b):
    """Short name of a tool call: the tool and its command or file."""
    inp = b.get("input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("skill") \
        or inp.get("description") or ""
    return f"{b.get('name') or 'tool'}: {_snip(detail, 60)}"


IMAGE_CHARS = 2722 * 3.5   # a 1080p frame measured at ~2.7k tokens; counted as text of that size


def _result_size(cc):
    """Rough size of a tool result in characters, so a call's growth can be split between results."""
    if isinstance(cc, str):
        return len(cc)
    n = 0
    for x in cc or []:
        if isinstance(x, dict):
            n += IMAGE_CHARS if x.get("type") == "image" else len(str(x.get("text") or ""))
    return n


def read_chat(path):
    """Model calls of one Claude Code transcript: usage, what each call ran, what came back."""
    calls, idx, names, seen, labels = [], {}, {}, set(), {}
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message")
        if not isinstance(m, dict):
            continue
        if d.get("type") == "assistant" and "usage" in m:
            mid = m.get("id")
            u = m["usage"]
            if not any(u.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                          "cache_creation_input_tokens", "output_tokens")):
                continue   # a synthetic message, not a model call
            if mid not in idx:
                idx[mid] = len(calls)
                calls.append({"cr": u.get("cache_read_input_tokens") or 0,
                              "cw": u.get("cache_creation_input_tokens") or 0,
                              "inp": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0,
                              "add": collections.Counter(), "ts": _iso_ts(d.get("timestamp")),
                              "ds": 0, "tools": 0, "model": m.get("model"), "items": [], "made": []})
            c = calls[idx[mid]]
            c["out"] = max(c["out"], m["usage"].get("output_tokens") or 0)
            for b in m.get("content") or []:
                if b.get("type") == "tool_use" and b.get("id") not in names:
                    n = b.get("name") or "tool"
                    c["tools"] += 1
                    if n == "Bash":
                        if DS_CMD_RE.search(str((b.get("input") or {}).get("command"))):
                            n = "ds output"
                            c["ds"] += 1
                        else:
                            n = "shell output"
                    names[b.get("id")] = n
                    labels[b.get("id")] = _use_label(b)
                    c["made"].append(labels[b.get("id")])
        elif d.get("type") == "user" and calls:
            cont = m.get("content")
            if isinstance(cont, list):
                for b in cont:
                    if b.get("type") == "tool_result" and b.get("tool_use_id") not in seen:
                        seen.add(b.get("tool_use_id"))
                        n = names.get(b.get("tool_use_id"), "tool")
                        cc = b.get("content")
                        if isinstance(cc, list) and any(isinstance(x, dict) and x.get("type") == "image" for x in cc):
                            n = "images"
                        calls[-1]["add"][n] += 1
                        calls[-1]["items"].append((n, labels.get(b.get("tool_use_id"), n), _result_size(cc)))
                    elif b.get("type") == "text":
                        calls[-1]["add"]["user / injected text"] += 1
                        calls[-1]["items"].append(("user / injected text", _snip(b.get("text")),
                                                   len(b.get("text") or "")))
            elif isinstance(cont, str):
                calls[-1]["add"]["user / injected text"] += 1
                calls[-1]["items"].append(("user / injected text", _snip(cont), len(cont)))
    return calls


def call_cost(c):
    return (c["inp"] * COST_W["in"] + c["cw"] * COST_W["cache_write"] + c["cr"] * COST_W["cache_read"]
            + c["out"] * COST_W["out"])


def chat_jobs(sid):
    """Jobs this chat owns that reached the project, with the lines the worker wrote (from the
    saved diffs) and the worker's own token use (from the ledger)."""
    worker = collections.defaultdict(lambda: [0, 0, 0])
    try:
        for line in open(LEDGER):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            w = worker[e.get("job")]
            w[0] += e.get("worker_out") or 0
            w[1] += e.get("secs") or 0
            w[2] += 1
    except OSError:
        pass
    out = []
    for jid in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        try:
            job = json.load(open(os.path.join(JOBS, jid, "job.json")))
        except (OSError, ValueError):
            continue
        if job.get("owner") != sid:
            continue
        st = job.get("lines")
        if st is None:
            st = diff_lines(os.path.join(JOBS, jid, "full.diff"))
        out.append({"id": jid, "status": job.get("status"), "started": job.get("started") or 0,
                    "landed": job.get("status") in ("landed", "approved"),
                    "files": st[0], "added": st[1], "removed": st[2], "chars": st[3],
                    "worker_out": worker[jid][0], "secs": worker[jid][1], "rounds": worker[jid][2]})
    return out


def diff_lines(path):
    """(files, added lines, removed lines, added characters) of a saved diff."""
    files = added = removed = chars = 0
    try:
        for l in open(path, errors="replace"):
            if l.startswith("+++ "):
                files += 1
            elif l.startswith("+") and not l.startswith("+++"):
                added += 1
                chars += len(l) - 1
            elif l.startswith("-") and not l.startswith("---"):
                removed += 1
    except OSError:
        pass
    return [files, added, removed, chars]


def delegation_account(calls, jobs):
    """Measured: what delegating cost Claude in this chat. Stated assumption: the least Claude
    would have paid to type the landed lines itself. Returns a dict of weighted-token figures."""
    n = len(calls)
    ctx = [c["cr"] + c["cw"] + c["inp"] for c in calls]
    # a context that shrinks by a third was compacted: nothing is carried past that point
    cuts = [i for i in range(1, n) if ctx[i] < ctx[i - 1] * 0.67]
    share = [(c["ds"] / c["tools"]) if c["tools"] else 0.0 for c in calls]

    def own_later(i):
        """Calls after call i that re-read what call i added and are Claude's own work (the ds
        share of a later call is already counted in full)."""
        end = next((k for k in cuts if k > i), n)
        return sum(1 - share[k] for k in range(i + 1, end))

    ds_calls = sum(share[i] * call_cost(calls[i]) for i in range(n))
    ds_carry = 0.0
    for i in range(n - 1):
        if not share[i]:
            continue
        delta = ctx[i + 1] - ctx[i]
        if delta > 0:
            ds_carry += share[i] * delta * COST_W["cache_read"] * own_later(i)
    landed = [j for j in jobs if j["landed"] and j["added"]]
    floor_out = floor_carry = floor_calls = 0.0
    for j in landed:
        tok = j["chars"] / CHARS_PER_TOKEN
        at = max([i for i in range(n) if calls[i]["ts"] and calls[i]["ts"] <= j["started"]] or [0])
        floor_out += tok * (COST_W["out"] + COST_W["cache_write"])
        floor_carry += tok * COST_W["cache_read"] * max(own_later(at) - 1, 0)
        floor_calls += max(j["files"], 1) * ctx[at] * COST_W["cache_read"]
    return {"ds_calls": ds_calls, "ds_carry": ds_carry, "ds_total": ds_calls + ds_carry,
            "n_ds_calls": sum(share), "landed": landed,
            "lines": sum(j["added"] for j in landed), "chars": sum(j["chars"] for j in landed),
            "files": sum(max(j["files"], 1) for j in landed),
            "floor_out": floor_out, "floor_carry": floor_carry, "floor_calls": floor_calls,
            "floor": floor_out + floor_carry + floor_calls}


def item_costs(calls, ctx):
    """Every single thing that entered the context (a tool result, an injected text, one call's own
    output), with its size in tokens and what re-reading it cost until the context was compacted.
    A call's growth is split between its results by their size in characters."""
    n = len(calls)
    cuts = [i for i in range(1, n) if ctx[i] < ctx[i - 1] * 0.67]
    out = []
    for i in range(n - 1):
        delta = ctx[i + 1] - ctx[i]
        if delta <= 0:
            continue
        later = next((k for k in cuts if k > i), n) - 1 - i
        own = min(calls[i]["out"], delta)
        made = ", ".join(calls[i]["made"]) or "text"
        out.append(("Claude's own output", f"call {i + 1} Claude wrote: {_snip(made, 60)}", own,
                    own * COST_W["cache_read"] * later))
        items = calls[i]["items"] or [("other", "other", 1)]   # results of call i's tool uses
        size = sum(max(x[2], 1) for x in items)
        for cat, label, chars in items:
            tok = (delta - own) * max(chars, 1) / size
            out.append((cat, f"call {i + 1}: {label}", tok, tok * COST_W["cache_read"] * later))
    return out


def cache_rebuilds(calls, ctx):
    """Calls that wrote most of an existing context to the cache again (the cache had expired),
    with the idle gap before them and what the rewrite cost above a cache read."""
    out = []
    for i in range(1, len(calls)):
        cr, prev = calls[i]["cr"], ctx[i - 1]
        # most of the old context was not read from the cache although it is still there
        if prev and cr < 0.5 * prev and ctx[i] >= 0.67 * prev:
            t0, t1 = calls[i - 1]["ts"], calls[i]["ts"]
            gap = (t1 - t0) if t0 is not None and t1 is not None else None
            lost = prev - cr
            out.append((i + 1, gap, lost, lost * (COST_W["cache_write"] - COST_W["cache_read"])))
    return out


def cost_detail(calls, ctx, reread):
    items = sorted(item_costs(calls, ctx), key=lambda x: -x[3])
    print("most expensive single items (size, re-reading cost until compaction or the end):")
    for cat, label, tok, c in items[:12]:
        print(f"  {fmt_num(c):>7} {100 * c / reread if reread else 0:3.0f}%  {fmt_num(tok):>6} tok  {label}")
    rb = cache_rebuilds(calls, ctx)
    if rb:
        tot = sum(r[3] for r in rb)
        print(f"cache rebuilds (cache expired, context written again): {len(rb)}, extra {fmt_num(tot)}")
        for no, gap, cw, extra in rb[:8]:
            g = f"{gap / 60:.0f} min idle" if gap is not None else "gap unknown"
            print(f"  call {no}: {g}, {fmt_num(cw)} tok rewritten, +{fmt_num(extra)}")
    else:
        print("cache rebuilds: none")


def cmd_cost(a):
    """What a Claude Code chat cost, measured from its transcript (API usage per call), in
    input-token equivalents (cache read 0.1x, cache write 1.25x, output 5x: the price ratios),
    what the re-read context was made of, and whether delegating paid for itself."""
    import glob
    sid = (a.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        die("ds cost SESSION_ID (or run it inside a Claude Code chat)")
    found = glob.glob(os.path.join(HOME, ".claude/projects/*", sid + "*.jsonl"))
    if not found:
        die(f"no transcript for session {sid}")
    if len(found) > 1:
        die(f"{sid} matches {len(found)} chats; give more of the id")
    sid = os.path.basename(found[0])[:-6]
    calls = read_chat(found[0])
    if not calls:
        die("the transcript has no model calls yet")
    n = len(calls)
    ctx = [c["cr"] + c["cw"] + c["inp"] for c in calls]
    total = sum(call_cost(c) for c in calls)
    grow, cost = collections.Counter(), collections.Counter()
    cost["starting prompt (system, tools, CLAUDE.md, memory)"] = ctx[0] * COST_W["cache_read"] * n
    for i in range(n - 1):
        delta = ctx[i + 1] - ctx[i]
        if delta <= 0:
            continue   # compaction or a cleared context
        own = min(calls[i]["out"], delta)
        keys = list(calls[i]["add"].elements()) or ["other"]
        later = n - 1 - i
        grow["Claude's own earlier output"] += own
        cost["Claude's own earlier output"] += own * COST_W["cache_read"] * later
        for k in keys:
            grow[k] += (delta - own) / len(keys)
            cost[k] += (delta - own) / len(keys) * COST_W["cache_read"] * later
    reread = sum(c["cr"] for c in calls) * COST_W["cache_read"]
    print(f"session {sid}: {n} model calls, context {fmt_num(ctx[0])} -> {fmt_num(ctx[-1])} tokens")
    print(f"cost {fmt_num(total)} weighted tokens = re-reading context {fmt_num(reread)} + "
          f"cache writes {fmt_num(sum(c['cw'] for c in calls) * COST_W['cache_write'])} + "
          f"output {fmt_num(sum(c['out'] for c in calls) * COST_W['out'])}")
    print("re-reading cost by what was in the context:")
    for k, v in cost.most_common(8):
        print(f"  {k:52} {fmt_num(v):>7}  {100 * v / reread if reread else 0:3.0f}%"
              + (f"  (+{fmt_num(grow[k])} tok)" if grow.get(k) else ""))
    if getattr(a, "detail", False):
        cost_detail(calls, ctx, reread)
    jobs = chat_jobs(sid)
    acc = delegation_account(calls, jobs)
    if not jobs and not acc["n_ds_calls"]:
        return
    pct = lambda v: f"{100 * v / total:.0f}%" if total else "0%"
    print(f"\ndelegation, measured: {len(jobs)} jobs, {len(acc['landed'])} landed with changes: "
          f"{acc['lines']} lines in {acc['files']} files written by the worker "
          f"({fmt_num(sum(j['worker_out'] for j in jobs))} worker output tokens, "
          f"{int(sum(j['secs'] for j in jobs) / 60)} min of worker time)")
    print(f"  delegating cost Claude {fmt_num(acc['ds_total'])} ({pct(acc['ds_total'])} of the chat): "
          f"{acc['n_ds_calls']:.0f} calls that ran ds {fmt_num(acc['ds_calls'])} "
          f"+ re-reading briefs and ds output later {fmt_num(acc['ds_carry'])}")
    if not acc["lines"]:
        print("  nothing landed, so nothing was saved")
        return
    print(f"least Claude would have paid to type those {acc['lines']} lines itself (assumes no reading, "
          f"no test runs, no mistakes, one call per file; {CHARS_PER_TOKEN} chars/token):")
    print(f"  {fmt_num(acc['floor'])} = writing {fmt_num(acc['floor_out'])} + re-reading them for the rest "
          f"of the chat {fmt_num(acc['floor_carry'])} + {acc['files']} calls {fmt_num(acc['floor_calls'])}")
    net = acc["floor"] - acc["ds_total"]
    if net > 0:
        print(f"=> saved at least {fmt_num(net)} weighted tokens: without ds this chat would have cost "
              f"{fmt_num(total + net)} or more, {100 * net / (total + net):.0f}% above what it did")
    else:
        print(f"=> a saving is not shown: delegating cost {fmt_num(-net)} more than typing the lines. It paid off "
              f"only if Claude alone would have spent more than that on reading, testing and fixing")


def _tool_short(part):
    nm = part.get("name") or part.get("tool") or "tool"
    inp = (part.get("state") or {}).get("input") or {}
    detail = (inp.get("command") or inp.get("path") or inp.get("filePath")
              or inp.get("pattern") or "")
    detail = " ".join(str(detail).split())
    if len(detail) > 100:
        detail = detail[:100] + "…"
    return f"  ▸ {nm} {detail}".rstrip()


def _tool_output(part):
    st = part.get("state") or {}
    if isinstance(st.get("output"), str):
        out = st["output"]
    else:
        out = "\n".join(c.get("text", "") for c in (st.get("content") or [])
                        if isinstance(c, dict) and c.get("type") == "text")
    return out.splitlines()[:6]


def cmd_log(a):
    job = load(a.job)
    sid = job.get("session")
    if not sid:
        die(f"job {a.job} has no session to export")
    # opencode truncates a piped export at 320 KiB; a file redirect is complete.
    fd, tmp = tempfile.mkstemp(prefix="ds-export-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            r = subprocess.run(["opencode", "session", "export", sid],
                               stdout=out, stderr=subprocess.PIPE, text=True)
        if r.returncode:
            msg = (r.stderr or "").strip()
            if not msg:
                try:
                    with open(tmp, encoding="utf-8") as f:
                        msg = f.read().strip()[:500]
                except OSError:
                    pass
            print(msg or "session export failed", file=sys.stderr)
            sys.exit(1)
        try:
            with open(tmp, encoding="utf-8") as f:
                data = json.load(f)
        except ValueError as e:
            print(f"ds: could not parse session export: {e}", file=sys.stderr)
            sys.exit(1)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    for m in data.get("messages") or []:
        t = m.get("type")
        if t == "user":
            if a.tools_only:
                continue
            print("── user")
            print((m.get("text") or "")[:400])
        elif t == "assistant":
            blocks = []
            for part in m.get("content") or []:
                if part.get("type") == "text" and not a.tools_only:
                    blocks.append(("text", part.get("text") or ""))
                elif part.get("type") == "tool":
                    blocks.append(("tool", part))
            if not blocks:
                continue
            print("── worker")
            for kind, val in blocks:
                if kind == "text":
                    print(val)
                else:
                    print(_tool_short(val))
                    for ln in _tool_output(val):
                        print("    " + ln)


def delegate_session(a):
    sid = a.session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    sid = sid.strip() if isinstance(sid, str) else ""
    if not sid:
        die("missing session id (pass --session ID or set CLAUDE_CODE_SESSION_ID)")
    return sid


def prune_delegates():
    now = time.time()
    try:
        names = os.listdir(DELEGATE)
    except OSError:
        return
    for name in names:
        path = os.path.join(DELEGATE, name)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > DELEGATE_TTL:
                os.remove(path)
        except OSError:
            pass


def write_marker_state(marker, st):
    os.makedirs(DELEGATE, exist_ok=True)
    tmp = marker + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, marker)


def delegate_status_block(st):
    c = cfg()
    if st is None:
        return ("ds delegation: OFF in this chat\n"
                "  Claude works directly. Turn on: /ds:on (Flash) or /ds:muse")
    model = resolve_model(st.get("model")) or c["model"]
    fb = (c.get("fallback_model") or "").strip()
    if fb == model:
        fb = other_model(model) or ""
    esc = (c.get("escalation_model") or "").strip()
    if esc == model:
        esc = other_model(model) or ""
    lines = ["ds delegation: ON in this chat",
             f"  worker      {short_model(model)}" + ("" if st.get("model") else " (default)"),
             f"  fallback    {short_model(fb) if fb else 'none'}   (provider fails or hits a limit)",
             f"  extra round {short_model(esc) if esc else 'none'}   (after the fix rounds fail)",
             f"  on failure  {'Sonnet subagent takes over' if c.get('takeover') == 'sonnet' else 'Claude asks you first'}",
             "  switch: /ds:flash or /ds:muse · off: /ds:off"]
    return "\n".join(lines)


def cmd_delegate(a):
    if getattr(a, "model_opt", None):
        a.model = a.model_opt
    sid = delegate_session(a)
    marker = os.path.join(DELEGATE, sid)
    st = read_marker(marker) if os.path.exists(marker) else None
    want = getattr(a, "model", None)
    if want and resolve_model(want) == want and "/" not in want:
        die(f"unknown model name {want!r}; known: {', '.join(cfg().get('models') or {})} or a provider/model id")
    if a.action == "status":
        print(delegate_status_block(st))
        return
    if a.action == "model":
        if not want:
            die("ds delegate model NAME (e.g. flash, muse)")
        if st is None:
            die("delegation is off in this chat; turn it on with ds delegate on --model " + want)
        st["model"] = want
        write_marker_state(marker, st)
        print(delegate_status_block(st))
        return
    if a.action == "off":
        try:
            os.remove(marker)
        except FileNotFoundError:
            pass
        except OSError as e:
            die(f"cannot disable delegation for {sid}: {e}")
        prune_delegates()
        print(delegate_status_block(None))
        return
    # on
    first = st is None or not st.get("shown")
    st = dict(st or {})
    if want:
        st["model"] = want
    st["shown"] = True            # the hook then won't inject the playbook a second time
    try:
        write_marker_state(marker, st)
    except OSError as e:
        die(f"cannot enable delegation for {sid}: {e}")
    prune_delegates()
    print(delegate_status_block(st))
    if first:
        print("\nRead these rules now; every brief and revise follows them.\n")
        print(playbook_text())
    else:
        print("\n(rules already shown in this chat: ds playbook prints them again)")


HANDOFF_DIR = os.path.join(HOME, ".local/state/ds-bridge/handoff")


def cmd_handoff(a):
    """Save Claude's notes for a fresh chat (stdin) to <dir>/.plan/HANDOFF.md with this chat's
    open jobs and delegation state. The SessionStart hook hands the file to the next chat when the
    user types /clear, so the long context is dropped instead of re-read on every call."""
    import glob
    sid = delegate_session(a)
    notes = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    if len(notes) < 40:
        die("ds handoff -C <dir> - <<'EOF' … EOF: notes on stdin: goal, decisions made, what is done, "
            "what is next, files that matter (short; the next chat sees nothing else)")
    d = os.path.abspath(os.path.expanduser(a.dir or os.getcwd()))
    path = os.path.join(d, ".plan", "HANDOFF.md")
    st = read_marker(os.path.join(DELEGATE, sid)) if os.path.exists(os.path.join(DELEGATE, sid)) else None
    rows, running = [], []
    for j in chat_jobs(sid):
        if j["status"] in ("landed", "approved", "rejected"):
            continue
        live = False
        try:
            live = pid_alive(json.load(open(os.path.join(RUNNING, j["id"] + ".json"))).get("pid"))
        except (OSError, ValueError):
            pass
        (running if live else rows).append(j["id"])
    auto = ["", "## State (written by ds handoff)",
            f"- delegation: {'on, model ' + (st.get('model') or 'default') if st is not None else 'off'}"]
    for jid in running:
        auto.append(f"- job {jid} still RUNNING: run `ds wait {jid} --until-done` with run_in_background")
    for jid in rows:
        auto.append(f"- job {jid} open (not landed): `ds status {jid}`, then approve / revise / reject")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# Handoff\n\n" + notes + "\n" + "\n".join(auto) + "\n")
    found = glob.glob(os.path.join(HOME, ".claude/projects/*", sid + ".jsonl"))
    os.makedirs(HANDOFF_DIR, exist_ok=True)
    pend = {"path": path, "from": sid, "t": time.time(), "delegate": st,
            "project": os.path.dirname(found[0]) if found else None, "jobs": rows}
    with open(os.path.join(HANDOFF_DIR, "pending.json"), "w") as f:
        json.dump(pend, f)
    tok = len(open(path).read()) // 4
    print(f"OK handoff saved: {path} (~{tok} tokens)" + (" — over 1.5k tokens, consider trimming" if tok > 1500 else ""))
    print("Tell the user: type /clear. The next chat starts with this handoff"
          + (" and delegation on" if st is not None else "") + " (within "
          + f"{int(cfg()['handoff_ttl_s']) // 3600} h). After a fresh `claude` instead, say: continue from {path}")


def cmd_playbook(a):
    print(playbook_text())


def cmd_list(a):
    if not os.path.isdir(JOBS):
        return
    ids = sorted(x for x in os.listdir(JOBS) if os.path.exists(os.path.join(JOBS, x, "job.json")))
    for jid in ids[-15:]:
        try:
            j = load(jid)
        except (SystemExit, ValueError):
            continue
        task = j.get("task") or ""
        if task.startswith("WORKSPACE:"):
            task = task.split("\n\n", 1)[-1]
        print(f"{jid}  {j['status']:8}  r{j['round']}  {proj(j)}  {' '.join(task.split())[:60]!r}")


def cmd_status(a):
    """One line per job: the running ones, or every job of one batch / one job id."""
    rows = []
    for jid in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        jp = os.path.join(JOBS, jid, "job.json")
        if not os.path.exists(jp):
            continue
        try:
            j = json.load(open(jp))
        except (OSError, ValueError):
            continue
        if a.job and a.job not in (jid, j.get("batch")):
            continue
        running = False
        try:
            running = pid_alive(json.load(open(os.path.join(RUNNING, jid + ".json"))).get("pid"))
        except (OSError, ValueError):
            pass
        if not a.job and not running:
            continue
        if running:
            try:
                pr = json.load(open(os.path.join(PROGRESS, jid + ".json")))
            except (OSError, ValueError):
                pr = {}
            el = int(time.time() - float(pr.get("started") or j.get("started") or time.time()))
            rows.append(f"{jid} running {short_model(j.get('model') or '')} {el // 60}:{el % 60:02d} "
                        f"step {pr.get('steps', 0)} · {pr.get('tools', 0)} tools · "
                        f"{fmt_num(pr.get('out_tokens') or 0)} tok · {_clip(pr.get('last') or '', 60)}")
        else:
            head = ""
            try:
                head = open(os.path.join(JOBS, jid, "last.txt")).readline().strip()
            except OSError:
                pass
            rows.append(head or f"{jid} {j.get('status')}")
    print("\n".join(rows) if rows else ("no such job or batch" if a.job else "no ds job is running"))


__all__ = [n for n in globals() if not n.startswith("__")]
