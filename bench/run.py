#!/usr/bin/env python3
"""Benchmark: Claude alone vs Claude + ds delegation, on cheap-ag's fixture and tasks.

Each run copies bench/repo to a fresh directory and runs one task prompt with
`claude -p` in one arm:
  baseline  delegation off (Claude works directly, as in a normal chat)
  ds        `ds delegate on` for the run's session id, so the hooks, the guard
            and the planner playbook apply exactly as in a delegating chat
Grading: the task's hidden tests are copied into test/ after the run and the
whole suite must pass (t6: only README.md may change; t7: the answer must cite
pricing.js and rounding, tree unchanged). One JSON line per run goes to
bench/results.jsonl; `--report` prints medians per task and arm.

Fixture, tasks and hidden tests: cheap-agent (MIT), see LICENSE-cheap-agent.
"""
import argparse
import filecmp
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.realpath(__file__))
DS = os.path.join(os.path.dirname(HERE), "ds")
RUNS = os.path.expanduser("~/.local/share/ds-bridge/bench-runs")
LEDGER = os.path.expanduser("~/.local/share/ds-bridge/ledger.jsonl")
RESULTS = os.path.join(HERE, "results.jsonl")
# Scoped permissions instead of --dangerously-skip-permissions: file tools (edits are
# accepted inside the run dir) plus the shell commands the tasks need.
ALLOWED = ["Read", "Glob", "Grep", "Edit", "Write", "Bash(node:*)", "Bash(npm test:*)",
           "Bash(npm run test:*)", "Bash(ds:*)", f"Bash({DS}:*)", "Bash(ls:*)", "Bash(cat:*)",
           "Bash(grep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(find:*)",
           "Bash(git diff:*)", "Bash(git status:*)"]
# Same lean, byte-stable prefix in both arms (cheap-agent's bench, ticket 23.1): no user settings,
# CLAUDE.md, plugins or MCP servers, no per-machine prompt sections; the same verification budget.
LEAN = ["--setting-sources", "project", "--exclude-dynamic-system-prompt-sections", "--strict-mcp-config"]
VERIFY_RULE = ("Tests: run only the test file or id the task concerns, once before if it names a failure "
               "and once after; never the whole suite; chain commands with &&.")
HOOKS = os.path.join(os.path.dirname(HERE), "hooks")


def ds_settings():
    """The ds arm's hooks, exactly as install.sh sets them: guard, delegate (rules), discipline."""
    g, dl, dc = (os.path.join(HOOKS, n) for n in ("guard.py", "delegate.py", "discipline.py"))
    cmd = lambda c: {"type": "command", "command": c}
    return json.dumps({"hooks": {
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [cmd(g)]},
                       {"matcher": "*", "hooks": [cmd(dc)]}],
        "PostToolUse": [{"matcher": "Bash", "hooks": [cmd(dc)]}],
        "UserPromptSubmit": [{"hooks": [cmd(dl)]}, {"hooks": [cmd(dc)]}],
        "SessionStart": [{"matcher": "compact", "hooks": [cmd(dl)]}]}})


def copy_fixture(dst):
    shutil.copytree(os.path.join(HERE, "repo"), dst)
    return dst


def snapshot_tree(d):
    out = {}
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in (".git", "node_modules")]
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, d)] = open(p, "rb").read()
    return out


def grade(task, d, before, answer):
    g = task.get("grade") or {}
    after = snapshot_tree(d)
    if g.get("unchanged"):
        if after != before:
            return False, "tree changed"
        if not re.search(g.get("answer", ""), answer or "", re.I):
            return False, "answer misses the expected references"
        return True, "answer ok, tree unchanged"
    if g.get("onlyFiles"):
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        extra = [k for k in changed if k not in g["onlyFiles"]]
        if extra:
            return False, "changed other files: " + ", ".join(extra[:5])
    hidden = os.path.join(HERE, "tasks", task["id"], "hidden")
    if os.path.isdir(hidden):
        for f in os.listdir(hidden):
            shutil.copy(os.path.join(hidden, f), os.path.join(d, "test", f))
    r = subprocess.run(["node", "--test"], cwd=d, capture_output=True, text=True, timeout=300)
    out = (r.stdout + r.stderr).splitlines()
    failed = [l.strip() for l in out if l.lstrip().startswith(("not ok", "✖"))][:3]
    counts = [l.strip() for l in out if re.match(r"^\s*ℹ (pass|fail) \d+", l)]
    return r.returncode == 0, " | ".join(counts + failed)


# OpenCode Go bills by subscription (ds shows $0), so the worker is priced at list rates
# per million tokens from OpenCode's catalog: input, cache read, output.
PRICES = {"default": (0.15, 0.003, 0.60)}   # deepseek-v4.1-flash


def worker_usage(d, t0):
    jobs, secs, cost, tin, tout = set(), 0, 0.0, 0, 0
    pin, pcache, pout = PRICES["default"]
    try:
        for line in open(LEDGER):
            e = json.loads(line)
            if e.get("ts", 0) >= t0 and os.path.realpath(e.get("dir", "")) == d:
                jobs.add(e["job"])
                secs += e.get("secs") or 0
                fresh = max(0, (e.get("worker_in") or 0))
                cache = e.get("worker_cache") or 0
                out = e.get("worker_out") or 0
                tin += fresh + cache
                tout += out
                cost += (fresh * pin + cache * pcache + out * pout) / 1e6
    except OSError:
        pass
    return len(jobs), secs, round(cost, 4), tin, tout


def run_one(task, arm, n, model, timeout_min):
    stamp = time.strftime("%m%d-%H%M%S")
    d = os.path.realpath(copy_fixture(os.path.join(RUNS, f"{stamp}-{task['id']}-{arm}-{n}")))
    before = snapshot_tree(d)
    sid = str(uuid.uuid4())
    args = ["claude", "-p", task["prompt"], "--output-format", "json", "--model", model,
            "--session-id", sid, "--permission-mode", "acceptEdits", *LEAN,
            "--append-system-prompt", VERIFY_RULE, "--allowedTools", *ALLOWED]
    if arm == "ds":
        subprocess.run([DS, "delegate", "on", "--session", sid], capture_output=True, check=True)
        # `ds delegate on` printed the rules to us, not to Claude, and marked them shown: clear the
        # mark so the delegate hook injects them into the first prompt, as in a real delegating chat
        with open(os.path.expanduser(f"~/.local/state/ds-bridge/delegate/{sid}"), "w") as f:
            f.write("{}")
        args += ["--settings", ds_settings()]
    t0 = time.time()
    try:
        r = subprocess.run(args, cwd=d, capture_output=True, text=True, timeout=timeout_min * 60)
        out = json.loads(r.stdout or "{}")
    except subprocess.TimeoutExpired:
        out = {"result": "", "timed_out": True}
    except ValueError:
        out = {"result": "", "error": "unparseable claude output"}
    finally:
        if arm == "ds":
            subprocess.run([DS, "delegate", "off", "--session", sid], capture_output=True)
    # the top-level "usage" is not the session total; modelUsage (per model) is
    mu = out.get("modelUsage") or {}
    u = {"input_tokens": sum(m.get("inputTokens", 0) for m in mu.values()),
         "cache_creation_input_tokens": sum(m.get("cacheCreationInputTokens", 0) for m in mu.values()),
         "cache_read_input_tokens": sum(m.get("cacheReadInputTokens", 0) for m in mu.values()),
         "output_tokens": sum(m.get("outputTokens", 0) for m in mu.values())} if mu else (out.get("usage") or {})
    ok, detail = grade(task, d, before, out.get("result", ""))
    jobs, wsecs, wcost, win, wout = worker_usage(d, t0)
    row = {"ts": int(t0), "task": task["id"], "arm": arm, "run": n, "model": model,
           "pass": ok, "detail": detail[:300],
           "claude_tokens": sum(u.get(k, 0) or 0 for k in
                                ("input_tokens", "cache_creation_input_tokens",
                                 "cache_read_input_tokens", "output_tokens")),
           "output_tokens": u.get("output_tokens", 0),
           "input_tokens": u.get("input_tokens", 0),
           "cache_write_tokens": u.get("cache_creation_input_tokens", 0),
           "cache_read_tokens": u.get("cache_read_input_tokens", 0),
           "model_usage": out.get("modelUsage"),
           "cost_usd": out.get("total_cost_usd"), "turns": out.get("num_turns"),
           "secs": round(time.time() - t0), "ds_jobs": jobs, "worker_secs": wsecs,
           "worker_cost_usd": wcost, "worker_tokens_in": win, "worker_tokens_out": wout,
           "timed_out": bool(out.get("timed_out")), "dir": d}
    with open(RESULTS, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"{row['task']} {arm} #{n}: {'PASS' if ok else 'FAIL'} tokens={row['claude_tokens']} "
          f"cost=${row['cost_usd'] or 0:.3f} worker=${wcost:.4f} {row['secs']}s ds_jobs={jobs} "
          f"({detail[:80]})",
          flush=True)
    return row


def report():
    rows = [json.loads(l) for l in open(RESULTS)] if os.path.exists(RESULTS) else []
    cells = {}
    for r in rows:
        cells.setdefault((r["task"], r["arm"]), []).append(r)
    print(f"{'task':16} {'arm':9} {'runs':>4} {'pass':>5} {'claude tokens':>14} {'claude $':>9} "
          f"{'worker $':>9} {'secs':>5}")
    totals = {}
    paired = {t for (t, a) in cells if (t, "baseline") in cells and (t, "ds") in cells}
    for (task, arm), rs in sorted(cells.items()):
        tok = statistics.median(r["claude_tokens"] for r in rs)
        cost = statistics.median((r["cost_usd"] or 0) for r in rs)
        secs = statistics.median(r["secs"] for r in rs)
        wcost = statistics.median((r.get("worker_cost_usd") or 0) for r in rs)
        if task not in paired:
            print(f"{task:16} {arm:9} {len(rs):>4}   (no pair yet, not in totals)")
            continue
        t = totals.setdefault(arm, {"tok": 0, "cost": 0, "wcost": 0, "pass": 0, "n": 0})
        t["tok"] += tok; t["cost"] += cost; t["wcost"] += wcost
        t["pass"] += sum(r["pass"] for r in rs); t["n"] += len(rs)
        print(f"{task:16} {arm:9} {len(rs):>4} {sum(r['pass'] for r in rs):>3}/{len(rs):<1} "
              f"{tok:>14,.0f} {cost:>9.3f} {wcost:>9.4f} {secs:>5.0f}")
    if "baseline" in totals and "ds" in totals:
        b, d = totals["baseline"], totals["ds"]
        print(f"\nsum of medians over {len(paired)} paired tasks   (DeepSeek not counted)")
        print(f"{'':16} {'claude tokens':>13} {'claude $':>10}   pass")
        for name, t in (("opus alone", b), ("opus + ds", d)):
            print(f"{name:16} {t['tok']:>13,.0f} {t['cost']:>10.3f}   {t['pass']}/{t['n']}")
        if d["cost"]:
            print(f"Claude usage: opus + ds costs {d['cost'] / b['cost']:.2f}x of opus alone "
                  f"({(1 - d['cost'] / b['cost']) * 100:+.0f}% saved)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tasks", default="", help="comma-separated id prefixes (default: all)")
    p.add_argument("--arms", default="baseline,ds")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--model", default="opus")
    p.add_argument("--timeout", type=int, default=25, help="minutes per run")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    if a.report:
        return report()
    tasks = json.load(open(os.path.join(HERE, "tasks.json")))
    want = [t for t in a.tasks.split(",") if t]
    if want:
        tasks = [t for t in tasks if any(t["id"].startswith(w) for w in want)]
    os.makedirs(RUNS, exist_ok=True)
    for t in tasks:
        for n in range(1, a.runs + 1):
            for arm in a.arms.split(","):
                run_one(t, arm, n, a.model, a.timeout)
    report()


if __name__ == "__main__":
    sys.exit(main())
