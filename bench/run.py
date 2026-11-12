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
# claude -p ends its turn when the model stops, so background jobs would be lost
FOREGROUND = ("Non-interactive benchmark run: start ds commands in the foreground "
              "(Bash timeout 600000), not with run_in_background.")


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


def worker_usage(d, t0):
    jobs, secs = set(), 0
    try:
        for line in open(LEDGER):
            e = json.loads(line)
            if e.get("ts", 0) >= t0 and os.path.realpath(e.get("dir", "")) == d:
                jobs.add(e["job"])
                secs += e.get("secs") or 0
    except OSError:
        pass
    return len(jobs), secs


def run_one(task, arm, n, model, timeout_min):
    stamp = time.strftime("%m%d-%H%M%S")
    d = os.path.realpath(copy_fixture(os.path.join(RUNS, f"{stamp}-{task['id']}-{arm}-{n}")))
    before = snapshot_tree(d)
    sid = str(uuid.uuid4())
    args = ["claude", "-p", task["prompt"], "--output-format", "json", "--model", model,
            "--session-id", sid, "--permission-mode", "acceptEdits",
            "--allowedTools", *ALLOWED]
    if arm == "ds":
        subprocess.run([DS, "delegate", "on", "--session", sid], capture_output=True, check=True)
        args += ["--append-system-prompt", FOREGROUND]
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
    u = out.get("usage") or {}
    ok, detail = grade(task, d, before, out.get("result", ""))
    jobs, wsecs = worker_usage(d, t0)
    row = {"ts": int(t0), "task": task["id"], "arm": arm, "run": n, "model": model,
           "pass": ok, "detail": detail[:300],
           "claude_tokens": sum(u.get(k, 0) or 0 for k in
                                ("input_tokens", "cache_creation_input_tokens",
                                 "cache_read_input_tokens", "output_tokens")),
           "output_tokens": u.get("output_tokens", 0),
           "cost_usd": out.get("total_cost_usd"), "turns": out.get("num_turns"),
           "secs": round(time.time() - t0), "ds_jobs": jobs, "worker_secs": wsecs,
           "timed_out": bool(out.get("timed_out")), "dir": d}
    with open(RESULTS, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"{row['task']} {arm} #{n}: {'PASS' if ok else 'FAIL'} tokens={row['claude_tokens']} "
          f"cost=${row['cost_usd'] or 0:.3f} {row['secs']}s ds_jobs={jobs} ({detail[:80]})",
          flush=True)
    return row


def report():
    rows = [json.loads(l) for l in open(RESULTS)] if os.path.exists(RESULTS) else []
    cells = {}
    for r in rows:
        cells.setdefault((r["task"], r["arm"]), []).append(r)
    print(f"{'task':16} {'arm':9} {'runs':>4} {'pass':>5} {'tokens (median)':>16} {'cost':>7} {'secs':>5}")
    totals = {}
    for (task, arm), rs in sorted(cells.items()):
        tok = statistics.median(r["claude_tokens"] for r in rs)
        cost = statistics.median((r["cost_usd"] or 0) for r in rs)
        secs = statistics.median(r["secs"] for r in rs)
        totals.setdefault(arm, []).append(tok)
        print(f"{task:16} {arm:9} {len(rs):>4} {sum(r['pass'] for r in rs):>3}/{len(rs):<1} "
              f"{tok:>16,.0f} {cost:>7.3f} {secs:>5.0f}")
    if "baseline" in totals and "ds" in totals:
        b, d = sum(totals["baseline"]), sum(totals["ds"])
        print(f"sum of medians: baseline {b:,.0f}  ds {d:,.0f}  ({(1 - d / b) * 100:+.0f}% saved)"
              if b else "")


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
