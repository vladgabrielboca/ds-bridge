"""ds - Claude -> OpenCode/DeepSeek execution bridge.

Claude plans and reviews; DeepSeek (OpenCode agent "worker") executes.
The bridge itself computes the diff from a shadow-git snapshot, so Claude
only ever reads: the worker's short report + a capped diff.

  ds run  [-C DIR] --check CMD [--files F] [--source TEXT|FILE] [--brief RESEARCH_ID] "short task"
                                                   quick task: the worker explores; passes land automatically
  ds run  [-C DIR] [-m MODEL] [-f FILE]... [--paths P ...]
          [--check CMD] [--fix-tries N] [--fix] "task"   new job
                                                   (--fix: worker writes a repro test; ds verifies fail→pass)
  ds init [-C DIR] [--refresh]                      job that writes AGENTS.md
  ds research [-C DIR] [--paths P ...] [--words N] "question"
                                                   read-only answer + refs, no diff
  ds revise JOB "feedback" [-f FILE]... [--check CMD]  same session, keeps DeepSeek's context
  ds diff   JOB [--full] [PATH...]                 re-show diff (optionally full / per path)
  ds approve JOB                                   accept (private copy: land its changes)
  ds reject  JOB                                   restore files to pre-job snapshot
                                                   (private copy: just delete it)
  ds rebase  JOB                                   fresh copy + job's changes, conflicts as markers
  ds batch [-C DIR] PLAN.md [--steps "1-2,3"] [--check CMD] [--brief ID] [--timeout S]
                                                   plan steps as parallel jobs, one block back
  ds reply   JOB a|"answer"                        answer a BLOCKED job's question
  ds wait    JOB [--until-done]                    result of a job that was still RUNNING
  ds list                                          recent jobs
  ds status [JOB|BATCH]                            running jobs, or the jobs of one batch, one line each
  ds stats [--days N | --all]                      worker tokens and measured speeds from the ledger
  ds cost [SESSION_ID]                             what a Claude chat cost, measured from its transcript
  ds log   JOB [--tools-only]                      readable session transcript
  ds delegate on|off|status [--session ID]         per-chat delegation mode
  ds playbook                                      the planner rules + brief template

Briefs are linted (config "brief_lint", default true): a run task needs the
sections GOAL / FILES / STEPS (numbered) / DO NOT / DONE WHEN, at least
"brief_min_chars" characters (default 300), and none of the vague words in
VAGUE_WORDS outside backticks and the DO NOT section. A revise needs numbered
points. A failing brief is refused before anything runs, with the template.

--paths limits the snapshot/diff/reject scope to those paths, relative to -C.

Every emit() appends one line to <STATE>/ledger.jsonl (tokens, emitted
characters, seconds, speeds, check result); ds stats totals it. A desktop notification is sent when a run or revise
finishes (config "notify", default true; at least "notify_min_s" seconds,
default 15).

--check runs CMD with bash -c in the job dir after every worker call. On a
non-zero exit (or a timeout, config check_timeout_s, default 600) the worker is
asked to fix the cause in the same session and the check is re-run, at most
fix_tries times (config "fix_tries", default 2; override with --fix-tries).
The job report shows PASS/FAIL, and the diff includes any auto-fix changes.
revise reuses the stored check unless --check replaces it.

A run that reaches soft_timeout_frac * timeout_s (config "soft_timeout_frac",
default 0.8) is stopped early; the same session is asked for a final report with
its own time limit (config "wrapup_s", default 180), and the report says the
soft deadline was hit. With --paths, the bridge also lists changes outside the
job's paths and shows them as a WARNING (config "outside_ignore", default
[".git", "__pycache__", "media*", "*.log", "node_modules", ".venv"]).

If OpenCode gives up because the provider is overloaded or rate-limited, ds
waits (config "overload_wait_s", default [30, 90]) and continues the same
session, at most "overload_retries" times (default 2)."""
import hashlib, json, os, signal, subprocess, sys, time, argparse, shutil, queue, threading, fcntl
import fnmatch, re, tempfile, base64, collections, urllib.request, urllib.parse, secrets
import contextlib, io
from .core import *  # noqa: F401,F403
from .workspace import *  # noqa: F401,F403
from .runner import *  # noqa: F401,F403
from .results import *  # noqa: F401,F403
from .jobs import *  # noqa: F401,F403
from .batch import *  # noqa: F401,F403
from .info import *  # noqa: F401,F403


def main():
    install_signal_handlers()
    p = argparse.ArgumentParser(prog="ds", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    r = s.add_parser("run"); r.add_argument("-C"); r.add_argument("-m")
    r.add_argument("-f", action="append"); r.add_argument("--paths", nargs="+")
    r.add_argument("--check"); r.add_argument("--fix-tries", type=int)
    r.add_argument("--fix", action="store_true")
    r.add_argument("--files"); r.add_argument("--source"); r.add_argument("--brief")
    r.add_argument("--timeout", type=int)
    r.add_argument("task", nargs="?")
    r.set_defaults(fn=cmd_run)
    r = s.add_parser("init"); r.add_argument("-C")
    r.add_argument("--refresh", action="store_true")
    r.add_argument("--timeout", type=int); r.set_defaults(fn=cmd_init)
    r = s.add_parser("research"); r.add_argument("-C"); r.add_argument("-m")
    r.add_argument("-f", action="append"); r.add_argument("--paths", nargs="+")
    r.add_argument("--words", type=int); r.add_argument("--timeout", type=int)
    r.add_argument("question", nargs="?"); r.set_defaults(fn=cmd_research)
    r = s.add_parser("revise"); r.add_argument("job"); r.add_argument("feedback")
    r.add_argument("-f", action="append"); r.add_argument("--check"); r.add_argument("--timeout", type=int)
    r.set_defaults(fn=cmd_revise)
    r = s.add_parser("diff"); r.add_argument("job"); r.add_argument("--full", action="store_true")
    r.add_argument("paths", nargs="*"); r.set_defaults(fn=cmd_diff)
    r = s.add_parser("approve"); r.add_argument("job", nargs="+"); r.set_defaults(fn=cmd_approve)
    r = s.add_parser("rebase"); r.add_argument("job"); r.set_defaults(fn=cmd_rebase)
    r = s.add_parser("reply"); r.add_argument("job"); r.add_argument("answer")
    r.add_argument("--check"); r.add_argument("--timeout", type=int); r.add_argument("-f", action="append")
    r.set_defaults(fn=cmd_reply)
    r = s.add_parser("batch"); r.add_argument("-C"); r.add_argument("-m"); r.add_argument("plan")
    r.add_argument("--steps"); r.add_argument("--check"); r.add_argument("--source")
    r.add_argument("--brief"); r.add_argument("--timeout", type=int)
    r.add_argument("--parallel", type=int); r.set_defaults(fn=cmd_batch)
    r = s.add_parser("wait"); r.add_argument("job"); r.add_argument("--max", type=int)
    r.add_argument("--until-done", action="store_true", help="no time limit; run it with Bash run_in_background")
    r.set_defaults(fn=cmd_wait)
    r = s.add_parser("reject"); r.add_argument("job")
    r.add_argument("--force", action="store_true"); r.set_defaults(fn=cmd_reject)
    r = s.add_parser("cost"); r.add_argument("session", nargs="?"); r.set_defaults(fn=cmd_cost)
    s.add_parser("list").set_defaults(fn=cmd_list)
    r = s.add_parser("status"); r.add_argument("job", nargs="?"); r.set_defaults(fn=cmd_status)
    r = s.add_parser("stats"); r.add_argument("--days", type=int)
    r.add_argument("--all", action="store_true"); r.set_defaults(fn=cmd_stats)
    r = s.add_parser("log"); r.add_argument("job")
    r.add_argument("--tools-only", action="store_true"); r.set_defaults(fn=cmd_log)
    r = s.add_parser("delegate")
    r.add_argument("action", choices=["on", "off", "status", "model"])
    r.add_argument("model", nargs="?")
    r.add_argument("--model", dest="model_opt")
    r.add_argument("--session")
    r.set_defaults(fn=cmd_delegate)
    s.add_parser("playbook").set_defaults(fn=cmd_playbook)
    a = p.parse_args()
    if os.environ.get("DS_STDIN"):
        sys.stdin = open(os.environ["DS_STDIN"])
    if (a.cmd in ("run", "revise", "reply", "research", "batch") and not os.environ.get("DS_CHILD")
            and int(cfg().get("wait_s") or 0) > 0):
        sys.exit(detach_and_wait(sys.argv, int(cfg()["wait_s"])))
    if os.environ.get("DS_CHILD"):
        import atexit
        atexit.register(finish_child)
    if getattr(a, "f", None):
        # opencode resolves -f against the job dir ($PWD); make paths absolute
        # from the caller's cwd and refuse missing files before any job starts.
        a.f = [os.path.abspath(os.path.expanduser(f)) for f in a.f]
        for f in a.f:
            if not os.path.isfile(f):
                die(f"-f {f}: no such file")
    if a.cmd == "research" and a.question is None:
        if a.paths and len(a.paths) > 1:
            a.question = a.paths.pop()
        else:
            die("research needs a question (or - for stdin)")
    if a.cmd == "run" and a.task is None:
        # --paths uses nargs="+"; when it comes before the task, argparse
        # greedily takes the task too. The task is the last value it took.
        if a.paths and len(a.paths) > 1:
            a.task = a.paths.pop()
        else:
            die("missing task (pass a string or - for stdin)")
    a.fn(a)


__all__ = [n for n in globals() if not n.startswith("__")]
