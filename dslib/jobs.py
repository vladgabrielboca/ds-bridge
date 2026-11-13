"""ds-bridge: the job commands: run, research, revise, reply, wait, approve, reject, rebase, diff, init."""
import hashlib, json, os, signal, subprocess, sys, time, argparse, shutil, queue, threading, fcntl
import fnmatch, re, tempfile, base64, collections, urllib.request, urllib.parse, secrets
import contextlib, io
from .core import *  # noqa: F401,F403
from .workspace import *  # noqa: F401,F403
from .runner import *  # noqa: F401,F403
from .results import *  # noqa: F401,F403
DS_BIN = os.path.join(ROOT, "ds")
INIT_BASE = (
    "Explore this project briefly (top-level layout, build/test/lint commands from "
    "config files such as package.json, pyproject.toml, Makefile, justfile, "
    "Cargo.toml) and write AGENTS.md at the project root, at most 40 lines: "
    "one-line purpose; layout (key dirs/files, one line each); exact commands to "
    "build, test, lint, run; code conventions you can see (language version, "
    "formatter, naming, test style); things not to touch (generated files, "
    "vendored code, large media). Facts only, no guesses; omit a section if unknown.")
INIT_REFRESH = (
    "Update the existing AGENTS.md at the project root, keeping correct lines. "
    "Explore this project briefly (top-level layout, build/test/lint commands from "
    "config files such as package.json, pyproject.toml, Makefile, justfile, "
    "Cargo.toml) and rewrite AGENTS.md at the project root, at most 40 lines: "
    "one-line purpose; layout (key dirs/files, one line each); exact commands to "
    "build, test, lint, run; code conventions you can see (language version, "
    "formatter, naming, test style); things not to touch (generated files, "
    "vendored code, large media). Facts only, no guesses; omit a section if unknown.")


# cheap-agent ticket 52: "test first, see it fail" made workers revert correct fixes or burn turns
# hunting a failing test. Fix first, then a test; ds itself runs it against the old code.
FIX_NOTE = ("\n\nBUG FIX RULE: make the fix as soon as you know it. Then write a small test that "
            "shows the problem (project's test style and folder) and covers every case the task "
            "or SOURCE names (each value, order, boundary; empty/zero/missing inputs where they "
            "apply), asserting the exact expected values, not just \"does not crash\". Run it with "
            "the task's check command. Do not try to make it fail yourself and never undo or "
            "revert the fix to check it: ds runs your test against the original code after you "
            "finish. Keep both the fix and the test. Add one line to your final report block: "
            "`REPRO: <exact command that runs only your new test>` (or `REPRO: none: <why>` if "
            "it cannot be shown by a test).")


COPY_NOTE = ("WORKSPACE: you work in a git worktree of the project at {w} (the current "
             "directory). Never read or write the original at {d}; use paths relative to "
             "the current directory. Ignored folders (media, renders, .venv) are links to the "
             "project's own: read them freely; renders you write there land in the project. "
             "Never commit.\n\n")


def note_jid(jid):
    """Tell a detaching parent (ds run in the foreground) which job this child runs."""
    out = os.environ.get("DS_OUT")
    if out:
        try:
            with open(out + ".jid", "w") as f:
                f.write(jid)
            open(os.path.join(JOBS, jid, "last.txt.pending"), "w").close()
        except OSError:
            pass


WAIT_NEXT = ("next: ds wait {jid} --until-done  (Bash run_in_background: true; one notice when it ends; "
             "end your turn meanwhile, don't poll)")


def detach_and_wait(argv, wait_s):
    """Run the command in a detached child; wait up to wait_s for it, then print its block, or a
    RUNNING line so Claude can `ds wait JOB` instead of the Bash tool timing out."""
    os.makedirs(os.path.join(STATE, "out"), exist_ok=True)
    out = os.path.join(STATE, "out", f"{os.getpid()}-{int(time.time())}.txt")
    env = dict(os.environ, DS_CHILD="1", DS_OUT=out)
    if "-" in argv[1:]:
        data = sys.stdin.read()
        with open(out + ".in", "w") as f:
            f.write(data)
        env["DS_STDIN"] = out + ".in"
    with open(out, "w") as fh:
        child = subprocess.Popen([sys.executable, DS_BIN, *argv[1:]], env=env,
                                 stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                 start_new_session=True, cwd=os.getcwd())
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if child.poll() is not None:
            sys.stdout.write(open(out).read())
            for ext in ("", ".jid", ".in"):
                try:
                    os.remove(out + ext)
                except OSError:
                    pass
            return child.returncode
        time.sleep(1)
    jid = open(out + ".jid").read().strip() if os.path.exists(out + ".jid") else "?"
    print(f"RUNNING {jid} (still working after {int(time.time() - t0)}s); " + WAIT_NEXT.format(jid=jid))
    return 0


def finish_child():
    """In the detached child: save the result block for ds wait."""
    out = os.environ.get("DS_OUT")
    if not out or not os.path.exists(out + ".jid"):
        return
    try:
        sys.stdout.flush()
        jid = open(out + ".jid").read().strip()
        shutil.copy(out, os.path.join(JOBS, jid, "last.txt"))
        os.remove(os.path.join(JOBS, jid, "last.txt.pending"))
    except OSError:
        pass


def claim_job(jid, d, paths, isolated=False):
    os.makedirs(RUNNING, exist_ok=True)
    with open(os.path.join(RUNNING, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not isolated:
            check_overlap(d, paths)
        write_marker(jid, d, paths, isolated)


def new_job_id():
    base = time.strftime("%m%d-%H%M%S")
    n = 0
    while True:
        jid = base if n == 0 else f"{base}-{n}"
        try:
            os.makedirs(os.path.join(JOBS, jid))
            return jid
        except FileExistsError:
            n += 1


def cmd_init(a):
    d = os.path.realpath(a.C or os.getcwd())
    target = os.path.join(d, "AGENTS.md")
    exists = os.path.exists(target)
    if exists and not a.refresh:
        die("AGENTS.md exists; use --refresh to rewrite it")
    task = INIT_REFRESH if a.refresh else INIT_BASE
    ns = argparse.Namespace(C=d, m=None, paths=["AGENTS.md"], check=None,
                            fix_tries=None, f=None, task=task, timeout=a.timeout,
                            internal=True)
    cmd_run(ns)


QUICK_TASK = ("TASK: {task}\nFILES: {files}\nCHECK: `{check}` (ds runs it after you finish; "
              "run it yourself first)\n")
MAX_SOURCE = 8000
MAX_BRIEF = 6000


def _text_or_file(value, d, cap):
    """A literal text, or the contents of a file path (relative to d) holding it."""
    if not value:
        return None
    v = value.strip()
    p = os.path.join(d, os.path.expanduser(v)) if "\n" not in v and len(v) < 512 else None
    if p and os.path.isfile(p):
        v = open(p, errors="replace").read().strip()
    return v[:cap] + (f"\n[… cut at {cap} characters]" if len(v) > cap else "")


def _brief_text(value, d):
    """--brief: a research job id (its saved answer) or the brief text itself."""
    if not value:
        return None
    v = value.strip()
    f = os.path.join(JOBS, v, "answer.txt")
    if re.fullmatch(r"\d{4}-\d{6}(-\d+)?", v):
        if not os.path.exists(f):
            die(f"--brief {v}: no saved research answer with that id")
        return open(f).read().strip()[:MAX_BRIEF]
    return _text_or_file(v, d, MAX_BRIEF)


def cmd_run(a):
    c = cfg()
    d = os.path.realpath(a.C or os.getcwd())
    if d in (HOME, "/"):
        die("refusing to run in $HOME or / ; pass -C <project dir>")
    task = a.task if a.task != "-" else sys.stdin.read()
    full = bool(brief_sections(task)) and not getattr(a, "internal", False)
    if not getattr(a, "internal", False) and not full:
        # Quick task: a few plain lines plus a check; the worker explores the code itself.
        if not task.strip():
            die("empty task")
        if not a.check:
            refuse_brief("run", ["a short task needs --check \"<cmd>\" (the worker and ds verify with it); "
                                 "without a check write a full brief with the sections below"])
        task = QUICK_TASK.format(task=task.strip(), files=getattr(a, "files", None) or
                                 "find them yourself; edit only what the task needs", check=a.check)
    elif c.get("brief_lint", True) and not getattr(a, "internal", False):
        probs = lint_brief(task)
        if probs:
            refuse_brief("run", probs)
    src = _text_or_file(getattr(a, "source", None), d, MAX_SOURCE)
    if src:
        task += "\nSOURCE (the user's words, verbatim):\n" + src + "\n"
    brief = _brief_text(getattr(a, "brief", None), d)
    if brief:
        task += "\nSCOUT BRIEF (from research; start here, verify anything marked unverified):\n" + brief + "\n"
    task = abs_outside(task, d)
    if a.check:
        a.check = abs_outside(a.check, d)
    paths = norm_paths(a.paths)
    budget = a.timeout if a.timeout is not None else c["job_budget_s"]
    os.makedirs(JOBS, exist_ok=True)
    jid = new_job_id()
    note_jid(jid)
    gc_landed()
    repo, git_note = None, None
    if want_copy(c, paths, force=getattr(a, "research", False)) or c.get("git_init", True):
        try:
            repo, git_note = ensure_repo(d)
        except SystemExit:
            shutil.rmtree(os.path.join(JOBS, jid), ignore_errors=True)
            raise
    gd = shadow_for(d)
    try:
        claim_job(jid, d, paths, isolated=want_copy(c, paths, force=getattr(a, "research", False)))
    except SystemExit:
        shutil.rmtree(os.path.join(JOBS, jid), ignore_errors=True)
        raise
    job = None
    post_taken = False
    work, copy_err = d, None
    if want_copy(c, paths, force=getattr(a, "research", False)):
        w, copy_err = make_copy(d, jid)
        if w:
            work = w
    if getattr(a, "fix", False):
        task = task + FIX_NOTE
    if c.get("worker_rules") == "message":
        # the agent keeps OpenCode's own system prompt (cheap-agent ticket 63: their custom agent
        # prompt made the worker slower); our rules travel at the top of the first message instead
        task = worker_rules_text() + "\n\n=====\n\n" + task
    if work != d:
        task = COPY_NOTE.format(w=work, d=d) + to_copy(task, d, work)
        if a.check:
            a.check = to_copy(a.check, d, work)
        if a.f:
            a.f = [to_copy(f, d, work) for f in a.f]
    try:
        base = snapshot(work, gd, f"pre {jid}", job_index(jid), jid, paths)
        job = {"id": jid, "dir": work, "shadow": gd, "base": base, "model": resolve_model(a.m) or chat_model() or c["model"],
               "agent": getattr(a, "agent", None) or c["agent"], "session": new_session_id(),
               "round": 1, "status": "open",
               "paths": paths, "task": task, "check": a.check,
               "owner": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
               **({"batch": os.environ["DS_BATCH"]} if os.environ.get("DS_BATCH") else {}),
               "started": time.time(), "fix": bool(getattr(a, "fix", False)),
               **({"project": d} if work != d else {}),
               **({"repo": repo} if repo else {}),
               **({"git_note": git_note} if git_note else {}),
               **({"copy_error": copy_err} if copy_err else {}),
               "fix_tries": a.fix_tries if a.fix_tries is not None else c["fix_tries"]}
        save(job)
        outside = outside_listing(work, paths) if paths else None
        want_note = c.get("excluded_note", True)
        excluded = excluded_listing(work, paths) if want_note else None
        res = run_with_check(job, task, session=job["session"], files=a.f or (),
                             budget_s=budget)
        if outside is not None:
            res["outside"] = outside_changes(outside, outside_listing(work, paths), work)
        if excluded is not None:
            note = excluded_note(excluded, excluded_listing(work, paths), c)
            if note:
                res["excluded_note"] = note
        job["post"] = snapshot(work, gd, f"post r{job['round']}", job_index(jid), jid, paths)
        post_taken = True
        if res.get("session"):
            job["session"] = res["session"]
        if job.get("fix") and not res["errors"]:
            res["repro"] = verify_repro(job, res.get("report"))
        save(job)
        if getattr(a, "research", False):
            finish_research(job, res, a.words)
        else:
            finish_job(job, res)
        notify_done(job, res)
    finally:
        if job is not None and not post_taken:
            try:
                job["post"] = snapshot(work, gd, f"post r{job['round']}",
                                       job_index(jid), jid, paths)
                save(job)
            except BaseException:
                pass
        mark_interrupted(jid)
        clear_marker(jid)
        cleanup_progress()


RESEARCH_TASK = (
    "QUESTION: {q}\n{scope}"
    "Answer in at most {w} words (REFS and VERIFIED lines do not count). "
    "Change nothing in the project. Reply with only the ANSWER/REFS/VERIFIED/CONFIDENCE/UNSURE block.")


def cmd_research(a):
    """Read-only job: a researcher answers one question so Claude plans without reading files."""
    c = cfg()
    q = a.question if a.question != "-" else sys.stdin.read()
    if not q or not q.strip():
        die("research needs a question")
    try:
        words = int(a.words if a.words is not None else c.get("research_words", 200))
    except (TypeError, ValueError):
        words = 200
    words = max(30, min(words, 600))
    d0 = os.path.realpath(a.C or os.getcwd())
    if a.paths:
        a.paths = [abs_outside(x.strip(), d0) for p_ in a.paths for x in p_.split(",") if x.strip()]
    scope = f"SCOPE: look only under {', '.join(a.paths)}; cite only files there.\n" if a.paths else ""
    task = RESEARCH_TASK.format(q=q.strip(), scope=scope, w=words)
    ns = argparse.Namespace(C=a.C, m=resolve_model(a.m), paths=None, check=None, fix_tries=0, f=a.f,
                            task=task, timeout=a.timeout, internal=True, research=True,
                            words=words, agent=c.get("research_agent", "researcher"))
    cmd_run(ns)


def finish_research(job, res, words):
    """Print the answer only; undo anything the researcher changed; close the job."""
    c = cfg()
    stat, _ = diff_text(job)
    changed = bool(stat.strip())
    rep = (res["report"] or "(researcher gave no answer)").strip()
    try:
        with open(os.path.join(JOBS, job["id"], "answer.txt"), "w") as f:
            f.write(rep)
    except OSError:
        pass
    cap = max(c["max_report_chars"], int(words) * 9 + 900)
    if len(rep) > cap:
        rep = rep[:cap] + " …[answer truncated]"
    tk = res["tokens"]
    out = [f"RESEARCH {job['id']} {res['secs']}s (pass it on: ds run --brief {job['id']})"]
    if res.get("limit"):
        lim = res["limit"]
        out.append("LIMIT: " + lim["message"] + (f" (resets {lim['resets']})" if lim.get("resets") else "")
                   + " — tell the user; don't retry")
    elif res["errors"]:
        out.append("ERRORS: " + " | ".join(e[:300] for e in res["errors"]))
    if res.get("failover"):
        out.append(f"FAILOVER: continued on {res['failover']['to']} ({res['failover']['reason']})")
    out.append(rep)
    if changed and job.get("project"):
        out.append("--- NOTE: the researcher changed files in its private copy; discarded")
        drop_copy(job)
        job["status"] = "approved"
        save(job)
        cleanup_shadow(job)
    elif changed:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_reject(argparse.Namespace(job=job["id"], force=False))
        out.append("--- WARNING: the researcher changed files; restored: "
                   + buf.getvalue().strip().splitlines()[-1][:200])
    else:
        drop_copy(job)
        job["status"] = "approved"
        save(job)
        cleanup_shadow(job)
    text = "\n".join(out) + "\n"
    sys.stdout.write(text)
    record_ledger(job, res, len(text))


def cmd_revise(a):
    job = load(a.job)
    if job["status"] == "landed":
        # revising a landed job: continue from the project as it is now
        if job.get("project"):
            w, err = make_copy(job["project"], f"{job['id']}-v{job['round'] + 1}")
            if not w:
                die(f"could not make a fresh copy: {err}")
            job["dir"] = w
            job["base"] = snapshot(w, job["shadow"], f"revise-base {job['id']}", job_index(job["id"]),
                                   job["id"], job.get("paths"))
            job["post"] = job["base"]
        job["status"] = "open"
        save(job)
    if job["status"] != "open":
        die(f"job {a.job} is {job['status']}")
    note_jid(job["id"])
    c = cfg()
    if a.feedback == "-":
        a.feedback = sys.stdin.read()
    if c.get("brief_lint", True) and not getattr(a, "reply", False):
        probs = lint_feedback(a.feedback)
        if probs:
            refuse_brief("revise", probs)
    budget = a.timeout if a.timeout is not None else c["job_budget_s"]
    paths = job.get("paths")
    claim_job(job["id"], proj(job), paths, isolated=bool(job.get("project")))
    post_taken = False
    try:
        # The chat that revises owns the job's status line from now on.
        job["owner"] = os.environ.get("CLAUDE_CODE_SESSION_ID") or job.get("owner") or ""
        if job.get("model_primary"):
            job["model"] = job.pop("model_primary")
        job["round"] += 1
        job["round_base"] = snapshot(job["dir"], job["shadow"], f"{job['id']} r{job['round']}",
                                     job_index(job["id"]), job["id"], paths)
        if a.check is not None:
            job["check"] = a.check
        msg = ("REVIEW FEEDBACK - revise your work. Fix exactly these points, nothing else, "
               "then reply with the same final report block (describe only this round's changes):\n"
               + (a.feedback if a.feedback != "-" else sys.stdin.read()))
        if not job.get("session"):
            msg = "ORIGINAL TASK:\n" + (job.get("task") or "") + "\n\n" + msg
            job["session"] = new_session_id()
            save(job)
        outside = outside_listing(job["dir"], paths) if paths else None
        want_note = c.get("excluded_note", True)
        excluded = excluded_listing(job["dir"], paths) if want_note else None
        res = run_with_check(job, msg, session=job["session"], files=a.f or (),
                             budget_s=budget)
        if outside is not None:
            res["outside"] = outside_changes(outside, outside_listing(job["dir"], paths), job["dir"])
        if excluded is not None:
            note = excluded_note(excluded, excluded_listing(job["dir"], paths), c)
            if note:
                res["excluded_note"] = note
        job["post"] = snapshot(job["dir"], job["shadow"], f"post r{job['round']}",
                               job_index(job["id"]), job["id"], paths)
        post_taken = True
        if job.get("fix") and not res["errors"]:
            res["repro"] = verify_repro(job, res.get("report"))
        save(job)
        finish_job(job, res)
        notify_done(job, res)
    finally:
        if not post_taken:
            try:
                job["post"] = snapshot(job["dir"], job["shadow"], f"post r{job['round']}",
                                       job_index(job["id"]), job["id"], paths)
                save(job)
            except BaseException:
                pass
        mark_interrupted(job["id"])
        clear_marker(job["id"])
        cleanup_progress()


def cmd_reply(a):
    """Answer a BLOCKED job's question (an option letter or a short answer) in the same session."""
    job = load(a.job)
    last = os.path.join(JOBS, job["id"], "last.txt")
    q = ""
    try:
        q = next((l[len("question: "):] for l in open(last) if l.startswith("question: ")), "")
    except OSError:
        pass
    ans = a.answer.strip()
    m = re.fullmatch(r"([a-d])\)?", ans, re.I)
    if m and q:
        opt = re.search(m.group(1).lower() + r"\)\s*(.+?)(?=\s+[a-d]\)\s|$)", q)
        if opt:
            ans = f"{m.group(1).lower()}) {opt.group(1).strip()}"
    a.feedback = (f"ANSWER to your question ({q[:300]}): {ans}\nContinue the task with this answer.")
    a.reply = True
    cmd_revise(a)


def cmd_wait(a):
    """Block until a detached job finishes (at most --max seconds; no limit with --until-done, which
    is meant for a background Bash call), then print its result block."""
    jid = a.job
    last = os.path.join(JOBS, jid, "last.txt")
    marker = os.path.join(RUNNING, jid + ".json")
    if not os.path.isdir(os.path.join(JOBS, jid)):
        die(f"no job {jid}")
    # --until-done still has a ceiling: a child killed hard (SIGKILL, reboot) leaves its .pending file
    limit = (int(cfg().get("until_done_max_s", 14400)) if a.until_done else
             a.max if a.max is not None else cfg().get("wait_s", 540))
    t0 = time.time()
    while time.time() - t0 < limit:
        if not os.path.exists(marker) and not os.path.exists(last + ".pending"):
            break
        time.sleep(2)
    if os.path.exists(marker) or os.path.exists(last + ".pending"):
        print(f"RUNNING {jid} ({int(time.time() - t0)}s more); " + WAIT_NEXT.format(jid=jid))
        return
    try:
        sys.stdout.write(open(last).read())
    except OSError:
        die(f"no result for {jid} (was it started by ds run?)")


def cmd_diff(a):
    job = load(a.job)
    if job["status"] != "open":
        die(f"job {a.job} is {job['status']}; snapshot removed, nothing to diff")
    stat, body = diff_text(job, full=a.full, paths=a.paths)
    print(stat.rstrip() or "(no changes)")
    print(body)


def cmd_approve(a):
    if len(a.job) > 1:
        bad = 0
        for jid in a.job:
            r = subprocess.run([sys.executable, DS_BIN, "approve", jid],
                               capture_output=True, text=True)
            print(((r.stdout or "") + (r.stderr or "")).strip().splitlines()[0] if (r.stdout or r.stderr).strip()
                  else f"{jid}: no output")
            bad += r.returncode != 0
        sys.exit(1 if bad else 0)
    a.job = a.job[0]
    job = load(a.job)
    if job["status"] == "landed":
        job["status"] = "approved"
        save(job)
        cleanup_shadow(job)
        print(f"approved {a.job} (it had already landed)")
        return
    if job["status"] != "open":
        die(f"job {a.job} is {job['status']}; nothing to approve")
    extra = ""
    if job.get("project"):
        # whatever is in the copy now lands, including edits made after the worker finished
        # (a Sonnet takeover works in the copy, then approves)
        job["post"] = snapshot(job["dir"], job["shadow"], f"post approve {job['id']}",
                               job_index(job["id"]), job["id"], job.get("paths"))
        marked = leftover_markers(job)
        if marked:
            die(f"job {a.job} still has conflict markers in: {', '.join(marked)}; "
                f"ds revise {a.job} \"1. resolve the conflict markers in ...\" first")
        conflict = land(job)
        if conflict:
            print(f"CONFLICT {a.job}: its changes no longer apply cleanly to {job['project']}; "
                  "nothing was changed there.")
            print("\n".join(conflict.splitlines()[:15]))
            print(f"--- next: ds rebase {a.job} (fresh copy, conflict markers for the worker), "
                  f"then ds revise {a.job} \"1. resolve the conflict markers in ...\"; "
                  f"or ds reject {a.job}")
            sys.exit(1)
        n, skipped = job.get("copied_back") or (0, [])
        if n:
            extra += f"; copied back {n} untracked files (renders, media)"
        if skipped:
            extra += f"; kept the project's newer {len(skipped)}: " + ", ".join(skipped[:5])
        if job.get("left_out"):
            extra += (f"; NOT landed, outside the job's --paths ({len(job['left_out'])}): "
                      + ", ".join(job["left_out"][:5]) + " (if they are needed, run the step again "
                      "with those files in its --paths)")
        drop_copy(job)
        extra = f": landed in {job['project']}" + extra
    job["status"] = "approved"
    save(job)
    cleanup_shadow(job)
    print(f"approved {a.job}{extra}")


def cmd_rebase(a):
    """Fresh copy of the project, then the job's changes 3-way applied onto it; clashing
    hunks stay as conflict markers for `ds revise` to resolve."""
    job = load(a.job)
    if job["status"] != "open" or not job.get("project"):
        die(f"job {a.job} is not an open job with a private copy")
    jid = job["id"]
    conflicted = rebase_job(job)
    err = job.pop("_rebase_err", "")
    save(job)
    if err and not conflicted:
        print(f"rebased {jid}: patch did not apply: {err}")
    elif conflicted:
        print(f"rebased {jid} onto the current project; conflict markers in: " + ", ".join(conflicted))
        print(f"--- next: ds revise {jid} \"1. In <file>, resolve the conflict markers: ...\"")
    else:
        print(f"rebased {jid} onto the current project; no conflicts. ds approve {jid}")


def cmd_reject(a):
    job = load(a.job)
    if job["status"] == "landed" and job.get("project"):
        # undo a landed copy job: 3-way apply its patch in reverse to the project
        patch, names = _job_patch(job)
        d, gd = job["project"], job["shadow"]
        with open(gd + ".land.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ok, out = _apply_3way(gd, d, _reverse(gd, d, job), names,
                                  os.path.join(JOBS, job["id"], "land.index")) if patch.strip() else (True, "")
        if not ok:
            die(f"could not undo {a.job}: the files changed since it landed:\n{out[:600]}")
        job["status"] = "rejected"
        save(job)
        cleanup_shadow(job)
        print(f"rejected {a.job}: its landed changes were undone in {d}")
        return
    if job["status"] == "landed":
        job["status"] = "open"   # in place: the normal restore below undoes it
    if job["status"] != "open":
        die(f"job {a.job} is {job['status']}; nothing to reject")
    if job.get("project"):
        drop_copy(job)
        job["status"] = "rejected"
        save(job)
        cleanup_shadow(job)
        print(f"rejected {a.job}: private copy deleted; {job['project']} was never changed")
        return
    d, gd = job["dir"], job["shadow"]
    idx = job_index(job["id"])
    paths = job.get("paths")
    if not job.get("post"):
        print(f"ds: warning: job {a.job} has no post-run snapshot; "
              "restoring the whole scope to the pre-job state")
        stage_paths(gd, d, paths, idx)
        # files created since snapshot -> remove; changed/deleted -> restore
        diff_args = ["diff", "--cached", "--name-only", "-z",
                     "--diff-filter=A", job["base"]]
        if paths:
            diff_args += ["--", *paths]
        added = [f for f in git(gd, d, *diff_args, index=idx).split("\0") if f]
        for f in added:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
        if paths:
            # New paths are absent from base; check out only paths that base has.
            base_paths = [f for f in git(gd, d, "ls-tree", "-r", "-z", "--name-only",
                                        job["base"], "--", *paths, index=idx).split("\0") if f]
            if base_paths:
                git(gd, d, "checkout", job["base"], "--", *base_paths, index=idx)
        else:
            git(gd, d, "checkout", job["base"], "--", ".", index=idx)
        job["status"] = "rejected"
        save(job)
        cleanup_shadow(job)
        print(f"rejected {a.job}: restored {d} to pre-job state ({len(added)} new files removed)")
        return

    post = job["post"]
    diff_args = ["diff", "--name-status", "-z", "--no-renames", job["base"], post]
    if paths:
        diff_args += ["--", *paths]
    entries = [e for e in git(gd, d, *diff_args).split("\0") if e]
    files = [entries[i + 1] for i in range(0, len(entries) - 1, 2)
             if entries[i + 1]]

    restore, skipped = [], []
    for f in files:
        if worktree_blob(gd, d, f) == commit_blob(gd, d, post, f):
            restore.append(f)
        else:
            skipped.append(f)
    force = bool(getattr(a, "force", False))
    if force:
        restore += skipped
        skipped = []

    restored, removed = 0, []
    for f in restore:
        if commit_blob(gd, d, job["base"], f) == "absent":
            try:
                os.remove(os.path.join(d, f))
                removed.append(f)
            except OSError:
                pass
        else:
            git(gd, d, "checkout", job["base"], "--", f, index=idx)
            restored += 1
    for f in removed:
        prune_empty_dirs(d, f)

    job["status"] = "rejected"
    save(job)
    cleanup_shadow(job)
    print(f"rejected {a.job}: restored {restored}, removed {len(removed)} new files")
    if skipped:
        names = ", ".join(skipped[:10])
        if len(skipped) > 10:
            names += " …"
        print(f"skipped {len(skipped)} changed after the job (use --force): {names}")


__all__ = [n for n in globals() if not n.startswith("__")]
