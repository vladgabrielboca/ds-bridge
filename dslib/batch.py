"""ds-bridge: ds batch: a plan's steps as parallel jobs."""
import hashlib, json, os, signal, subprocess, sys, time, argparse, shutil, queue, threading, fcntl
import fnmatch, re, tempfile, base64, collections, urllib.request, urllib.parse, secrets
import contextlib, io
from .core import *  # noqa: F401,F403
from .workspace import *  # noqa: F401,F403
from .runner import *  # noqa: F401,F403
from .results import *  # noqa: F401,F403
from .jobs import *  # noqa: F401,F403


PLAN_HELP = ("plan format: '# title', optional shared context (a 'check: `cmd`' line there is the "
             "integration check), then per step '## <id> <title>' with 'files: a, b', 'do: ...', "
             "'done-when: `cmd`' and optional 'needs: <ids>'")


def parse_plan(text):
    """{title, context, check, steps: [{id, title, files, do, done, needs, text}]}; dies on bad format."""
    title, ctx, steps, cur, key = None, [], [], None, None
    for line in text.splitlines():
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
            continue
        m = re.match(r"^##\s+(?:step[- ]?(?=\d)|s(?=\d))?([\w.]+)\s*(.*)$", line)
        if m:
            cur = {"id": m.group(1), "title": m.group(2).strip(), "files": "", "do": "", "done": "",
                   "needs": "", "text": line}
            steps.append(cur)
            key = None
            continue
        if cur is None:
            if title is not None:
                ctx.append(line)
            continue
        cur["text"] += "\n" + line
        kv = re.match(r"^(files|do|done-when|needs):\s?(.*)$", line)
        if kv:
            key = {"done-when": "done"}.get(kv.group(1), kv.group(1))
            cur[key] = kv.group(2).strip()
        elif key and re.match(r"^\s{2,}\S", line):
            cur[key] += " " + line.strip()
    if title is None or not steps:
        die("plan needs a '# title' and at least one '## <id> step'; " + PLAN_HELP)
    context = "\n".join(ctx).strip()
    m = re.search(r"^check:\s*`([^`]+)`", context, re.M)
    for st in steps:
        if not st["files"]:
            die(f"step {st['id']} has no files: line (needed to run steps in parallel); " + PLAN_HELP)
    ids = [st["id"] for st in steps]
    if len(set(ids)) != len(ids):
        die("duplicate step ids in the plan")
    return {"title": title, "context": context, "check": m.group(1) if m else None, "steps": steps}


def select_groups(plan, spec):
    """'1-2,3' -> [[1,2],[3]] (ranges follow the plan's order); default: every step its own group."""
    ids = [st["id"] for st in plan["steps"]]
    by = {st["id"]: st for st in plan["steps"]}
    if not spec:
        return [[st] for st in plan["steps"]]
    groups = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a_, b_ = part.split("-", 1)
            if a_ not in by or b_ not in by:
                die(f"--steps {part}: unknown step id")
            i, j = ids.index(a_), ids.index(b_)
            groups.append([by[x] for x in ids[i:j + 1]])
        else:
            if part not in by:
                die(f"--steps {part}: unknown step id")
            groups.append([by[part]])
    return groups


def _files(group):
    out = []
    for st in group:
        out += [f.strip().rstrip("/") for f in st["files"].split(",") if f.strip()]
    return out


def _overlap(a_, b_):
    for x in a_:
        for y in b_:
            if x == y or x.startswith(y + "/") or y.startswith(x + "/") or x in (".", "./") or y in (".", "./"):
                return x
    return None


BATCH_TASK = ("{title}\n{context}\n\nYour part: step(s) {ids} of this plan (other steps run in parallel in "
              "other copies; do only yours):\n\n{steps}")


def cmd_batch(a):
    """Run a plan's step groups as parallel jobs (each in a private copy), land them, then run the
    integration check; print one compact block."""
    c = cfg()
    d = os.path.realpath(a.C or os.getcwd())
    cands = [a.plan] if os.path.isabs(a.plan) else [os.path.abspath(a.plan), os.path.join(d, a.plan)]
    plan_path = next((x for x in cands if os.path.isfile(x)), None)
    if not plan_path:
        die(f"plan file not found: {a.plan} (looked in the current folder and in {d})")
    plan = parse_plan(open(plan_path).read())
    groups = select_groups(plan, a.steps)
    bid = "b" + time.strftime("%m%d-%H%M%S")
    os.makedirs(os.path.join(JOBS, bid), exist_ok=True)
    note_jid(bid)
    t0 = time.time()
    labels = ["-".join([g[0]["id"], g[-1]["id"]]) if len(g) > 1 else g[0]["id"] for g in groups]
    gfiles = [_files(g) for g in groups]
    after = [[j for j in range(i) if _overlap(gfiles[j], gfiles[i])] for i in range(len(groups))]
    step_group = {st["id"]: i for i, g in enumerate(groups) for st in g}
    needs = []
    for i, g in enumerate(groups):
        n = set()
        for st in g:
            for x in re.split(r"[,\s]+", st["needs"]):
                if x and x in step_group and step_group[x] != i:
                    if step_group[x] > i:
                        die(f"step {st['id']} needs a later step {x}")
                    n.add(step_group[x])
        needs.append(sorted(n))
    def init(obj):
        obj.update({"id": bid, "title": plan["title"], "plan": plan_path, "project": d,
                    "owner": os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "started": int(t0),
                    "groups": [{"label": labels[i], "steps": [st["id"] for st in g], "needs": needs[i],
                                "job": None, "status": "queued"} for i, g in enumerate(groups)],
                    "check": a.check or plan["check"], "ended": None, "pid": os.getpid()})
    update_batch(bid, init)

    def set_group(i, **kw):
        def fn(obj):
            if i < len(obj.get("groups") or []):
                obj["groups"][i].update({k: v for k, v in kw.items() if v is not None or k != "job"})
        update_batch(bid, fn)

    results = [None] * len(groups)
    lock = threading.Lock()
    slots = threading.Semaphore(max(1, int(a.parallel or c.get("max_parallel", 6))))
    done_ev = [threading.Event() for _ in groups]
    stopping = threading.Event()    # set on SIGTERM (the board's x): start nothing new
    procs = []                      # the child ds processes, so a stop can pass it on

    def child(argv, task, env):
        """Run one child ds; (exit code, stdout, stderr). A stop SIGTERMs it, and the child's
        own handler then interrupts its OpenCode session."""
        p_ = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env, cwd=d)
        with lock:
            procs.append(p_)
        if stopping.is_set():
            p_.terminate()
        so, se = p_.communicate(task)
        return p_.returncode, so, se

    def stopped(i):
        results[i] = {"status": "STOPPED", "line": f"STOPPED step {labels[i]}", "block": ""}
        set_group(i, status="STOPPED")
        done_ev[i].set()

    def run_group(i):
        for j in after[i] + needs[i]:
            done_ev[j].wait()
        if stopping.is_set():
            return stopped(i)
        # a step may only build on a dependency that is in the project: OK alone is not enough
        failed_dep = next((j for j in needs[i] if (results[j] or {}).get("status") != "OK"
                           or not (results[j] or {}).get("landed")), None)
        if failed_dep is not None:
            dep = results[failed_dep] or {}
            why = dep.get("status") if dep.get("status") != "OK" else "passed but did not land"
            results[i] = {"status": "SKIPPED", "line": f"SKIPPED step {labels[i]}: needs step "
                          f"{labels[failed_dep]} ({why})", "block": ""}
            set_group(i, status="SKIPPED")
            done_ev[i].set()
            return
        g = groups[i]
        cmds = [m_ for st in g for m_ in re.findall(r"`([^`]+)`", st["done"])]
        check = " && ".join(dict.fromkeys(cmds)) or a.check or plan["check"]
        task = BATCH_TASK.format(title=plan["title"], context=plan["context"], ids=labels[i],
                                 steps="\n\n".join(st["text"] for st in g))
        argv = [sys.executable, DS_BIN, "run", "-C", d]
        if a.m:
            argv += ["-m", resolve_model(a.m)]
        if check:
            argv += ["--check", check]
        if a.source:
            argv += ["--source", a.source]
        if a.brief:
            argv += ["--brief", a.brief]
        if a.timeout:
            argv += ["--timeout", str(a.timeout)]
        argv += ["--paths", *gfiles[i], "-"]
        with slots:
            if stopping.is_set():
                return stopped(i)
            set_group(i, status="running")
            env = dict(os.environ, DS_CHILD="1", DS_BATCH=bid, DS_BATCH_GROUP=str(i))
            env.pop("DS_OUT", None)
            code, so, se = child(argv, task, env)
        if stopping.is_set():
            return stopped(i)
        out = (so or "") + (("\n" + se.strip()) if code and se.strip() else "")
        first = out.strip().splitlines()[0] if out.strip() else f"ERROR step {labels[i]}: no output"
        status = first.split()[0] if first.split() else "ERROR"
        if status not in ("OK", "FAIL", "BLOCKED", "CONFLICT", "PARTIAL", "PROVIDER", "ERROR", "LIMIT"):
            status = "ERROR"
        with lock:
            results[i] = {"status": status, "line": f"{first}  (step {labels[i]})", "block": out.strip(),
                          "landed": " landed" in first}
        words = first.split()
        jid = words[1] if len(words) > 1 and re.fullmatch(r"\d{4}-\d{6}(-\d+)?", words[1]) else None
        set_group(i, status=status, job=jid)
        done_ev[i].set()

    def stop_all():
        """SIGTERM (the board's x): stop the children, mark what never ran, end the batch."""
        stopping.set()
        with lock:
            live = [p_ for p_ in procs if p_.poll() is None]
        for p_ in live:
            p_.terminate()
        until = time.time() + 40         # a child's own stop waits up to 15 s on OpenCode
        for p_ in live:
            try:
                p_.wait(timeout=max(1, until - time.time()))
            except subprocess.TimeoutExpired:
                p_.kill()

        def fn(obj):
            for g_ in obj.get("groups") or []:
                if g_.get("status") in ("queued", "running"):
                    g_["status"] = "STOPPED"
            obj.update({"ended": int(time.time()), "integration": obj.get("integration") or "none",
                        "stopped": True})
        update_batch(bid, fn)

    integ, integ_lines, fix_line = "none", [], ""
    try:
        threads = [threading.Thread(target=run_group, args=(i,), daemon=True) for i in range(len(groups))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check = a.check or plan["check"]
        landed_any = any((r_ or {}).get("landed") for r_ in results)
        if check and landed_any and all((r_ or {}).get("status") in ("OK", "SKIPPED") for r_ in results):
            chk = run_check(d, check, c["check_timeout_s"])
            integ = "pass" if chk["pass"] else "fail"
            if not chk["pass"]:
                tail = error_tail(chk["output"], d)
                task = (f"Steps {', '.join(labels)} of the plan '{plan['title']}' were done by separate jobs and "
                        f"have all landed, but together they fail `{check}`:\n" + "\n".join(tail) +
                        "\nFix the cause so it passes, keeping every step's intent.")
                env = dict(os.environ, DS_CHILD="1", DS_BATCH=bid)
                env.pop("DS_OUT", None)
                _, so, _ = child([sys.executable, DS_BIN, "run", "-C", d, "--check", check,
                                  *(["-m", resolve_model(a.m)] if a.m else []), "-"], task, env)
                fix_out = (so or "").strip()
                fix_line = fix_out.splitlines()[0] if fix_out else "ERROR fix job: no output"
                if fix_line.startswith("OK") and " landed" in fix_line:
                    integ = "pass (after fix job)"
                else:
                    integ_lines = ["integration error:"] + ["  " + l_ for l_ in tail[:12]] + ["fix job:"] + \
                                  ["  " + l_ for l_ in fix_out.splitlines()[:12]]
        fw = fix_line.split()
        fix_jid = fw[1] if len(fw) > 1 and re.fullmatch(r"\d{4}-\d{6}(-\d+)?", fw[1]) else None

        def fin(obj):
            obj.update({"ended": int(time.time()), "integration": integ,
                        **({"fix_job": fix_jid} if fix_jid else {})})
        update_batch(bid, fin)
    except BaseException:
        stop_all()
        raise
    n_ok = sum(1 for r_ in results if r_ and r_["status"] == "OK")
    out = [f"BATCH {bid} groups={len(groups)} ok={n_ok} not_ok={len(groups) - n_ok} "
           f"integration={integ} {int(time.time() - t0)}s"]
    for r_ in results:
        out.append(r_["line"])
    if fix_line:
        out.append("fix: " + fix_line)
    # a landed OK job shows only its headline; anything else its whole block (Claude must act on it)
    unlanded = []
    for i, r_ in enumerate(results):
        if r_["status"] == "OK" and not r_.get("landed"):
            jid = r_["line"].split()[1] if len(r_["line"].split()) > 1 else "?"
            unlanded.append(jid)
            why = [l_ for l_ in r_["block"].splitlines() if l_.startswith(("warning:", "conflict:", "note:"))]
            out.append(f"--- step {labels[i]}: passed but NOT landed ({jid})")
            out += why[:3] or ["(no check passed, so it was not auto-landed)"]
        elif r_["status"] not in ("OK", "SKIPPED") and r_["block"]:
            out.append(f"--- step {labels[i]}")
            out.append(r_["block"])
    out += integ_lines
    all_ok = all((r_ or {}).get("status") == "OK" for r_ in results)
    if unlanded:
        out.append(f"next: review the lines above, then land them in one call: ds approve {' '.join(unlanded)}")
    elif all_ok and integ in ("none", "pass", "pass (after fix job)"):
        out.append("next: nothing to do: every step landed (undo one with ds reject JOB)")
    if any((r_ or {}).get("status") in ("FAIL", "PARTIAL", "CONFLICT", "BLOCKED")
           and "check=env" not in (r_ or {}).get("line", "") for r_ in results) or integ == "fail":
        record_outcome("notdone")
    elif all_ok and not unlanded and integ in ("none", "pass", "pass (after fix job)"):
        record_outcome("ok")
    sys.stdout.write("\n".join(out) + "\n")


__all__ = [n for n in globals() if not n.startswith("__")]
