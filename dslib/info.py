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
    dec = [e["dec_tok_s"] for e in new if e.get("dec_tok_s") is not None]
    ttfts = [e["ttft_ms"] for e in new if e.get("ttft_ms") is not None]
    parts = []
    if thr:
        parts.append(f"throughput median {int(round(_median(thr)))} tok/s (incl. first-token wait)")
    if dec:
        parts.append(f"decode median {int(round(_median(dec)))} tok/s (streamed steps)")
    if ttfts:
        parts.append(f"first token median {_median(ttfts) / 1000:.1f}s")
    if parts:
        print(f"speed: {' · '.join(parts)} ({len(new)} rounds)")
    print("What a chat cost Claude is measured from its transcript: ds cost [SESSION_ID]")


COST_W = {"in": 1.0, "cache_write": 1.25, "cache_read": 0.1, "out": 5.0}


def cmd_cost(a):
    """What a Claude Code chat cost, measured from its transcript (API usage per call, nothing
    estimated), in input-token equivalents (cache read 0.1x, cache write 1.25x, output 5x: the
    standard price ratios), and what the re-read context was made of."""
    import glob
    sid = (a.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        die("ds cost SESSION_ID (or run it inside a Claude Code chat)")
    found = glob.glob(os.path.join(HOME, ".claude/projects/*", sid + ".jsonl"))
    if not found:
        die(f"no transcript for session {sid}")
    calls, idx, names, seen = [], {}, {}, set()
    for line in open(found[0], errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message")
        if not isinstance(m, dict):
            continue
        if d.get("type") == "assistant" and "usage" in m:
            mid = m.get("id")
            if mid not in idx:
                u = m["usage"]
                idx[mid] = len(calls)
                calls.append({"cr": u.get("cache_read_input_tokens") or 0,
                              "cw": u.get("cache_creation_input_tokens") or 0,
                              "inp": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0,
                              "add": collections.Counter()})
            for b in m.get("content") or []:
                if b.get("type") == "tool_use":
                    n = b.get("name") or "tool"
                    if n == "Bash":
                        n = "ds output" if re.search(r"(?:^|[\s;&|/])ds\s+\w", str((b.get("input") or {}).get("command"))) \
                            else "shell output"
                    names[b.get("id")] = n
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
                    elif b.get("type") == "text":
                        calls[-1]["add"]["user / injected text"] += 1
            elif isinstance(cont, str):
                calls[-1]["add"]["user / injected text"] += 1
    if not calls:
        die("the transcript has no model calls yet")
    n = len(calls)
    ctx = [c["cr"] + c["cw"] + c["inp"] for c in calls]
    total = sum(c["inp"] * COST_W["in"] + c["cw"] * COST_W["cache_write"] + c["cr"] * COST_W["cache_read"]
                + c["out"] * COST_W["out"] for c in calls)
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
