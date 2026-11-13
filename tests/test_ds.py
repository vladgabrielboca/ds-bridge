#!/usr/bin/env python3
"""Checks for ds that need no worker and no network: python3 tests/test_ds.py"""
import os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, HERE)
import dslib.cli as ds  # noqa: E402  (the top module re-exports every lower one)

fails = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {detail}" if not cond else ""))
    if not cond:
        fails.append(name)


def sh(d, *cmd):
    subprocess.run(cmd, cwd=d, check=True, capture_output=True)


with tempfile.TemporaryDirectory() as top:
    top = os.path.realpath(top)
    proj = os.path.join(top, "videos", "proj")
    os.makedirs(os.path.join(top, "videos", ".venv", "bin"))
    os.makedirs(proj)
    open(os.path.join(top, "videos", ".venv", "bin", "python"), "w").close()
    open(os.path.join(top, "videos", "AUDIENCE.md"), "w").close()

    # ../ paths that leave the project become absolute; others stay
    out = ds.abs_outside("../.venv/bin/python a.py && ./x.sh ../nope ../proj/a.py", proj)
    check("abs_outside rewrites an existing outside path", out.startswith(top + "/videos/.venv/bin/python a.py"), out)
    check("abs_outside leaves a missing path", " ../nope " in out, out)
    check("abs_outside leaves a path back into the project", out.endswith("../proj/a.py"), out)
    check("abs_outside ignores text without ../", ds.abs_outside("python a.py", proj) == "python a.py")

    # plan parsing: word ids, numeric ids, s1/step-1 prefixes
    plan = ds.parse_plan("# t\nctx\ncheck: `true`\n\n## common shared helpers\nfiles: a.py\ndo: x\n\n"
                         "## 2 scenes\nfiles: b.py\ndo: y\nneeds: common\n\n## step-3 end\nfiles: c.py\ndo: z\n")
    check("plan ids", [s["id"] for s in plan["steps"]] == ["common", "2", "3"], str([s["id"] for s in plan["steps"]]))
    check("plan check line", plan["check"] == "true")

    # status after a soft deadline
    soft = {"errors": [], "soft_deadline": {"at": 1}, "check": {"pass": True}}
    check("soft deadline + DONE + passing check = OK", ds.job_status(soft, {"STATUS": "DONE"}) == "OK")
    check("soft deadline + MISSING = PARTIAL", ds.job_status(soft, {"STATUS": "DONE", "MISSING": "S09"}) == "PARTIAL")
    check("soft deadline without a check = PARTIAL",
          ds.job_status({"errors": [], "soft_deadline": {"at": 1}}, {"STATUS": "DONE"}) == "PARTIAL")
    check("failed check = FAIL", ds.job_status({"errors": [], "check": {"pass": False}}, {"STATUS": "DONE"}) == "FAIL")

    # speeds: throughput counts every step, decode only streamed ones
    r = ds.set_speeds({"thr_tokens": 6519, "thr_ms": 86400, "dec_tokens": 0, "dec_ms": 0})
    check("hidden reasoning: shown = throughput 75", (r["tok_s"], r["tok_kind"], r["dec_tok_s"]) == (75, "throughput", None), str(r))
    r = ds.set_speeds({"thr_tokens": 1000, "thr_ms": 10000, "dec_tokens": 1000, "dec_ms": 8000})
    check("streamed: shown = decode 125, throughput 100 kept", (r["tok_s"], r["tok_kind"], r["thr_tok_s"]) == (125, "decode", 100), str(r))
    r = ds.set_speeds({"thr_tokens": 1000, "thr_ms": 10000, "dec_tokens": 300, "dec_ms": 1000})
    check("mostly hidden: shown = throughput", (r["tok_s"], r["tok_kind"]) == (100, "throughput"), str(r))

    # copy-back: ignored output travels, a plain file outside --paths does not
    sh(proj, "git", "init", "-q")
    open(os.path.join(proj, ".gitignore"), "w").write("built.py\n")
    open(os.path.join(proj, "a.py"), "w").write("x = 1\n")
    work = os.path.join(top, "work")
    sh(top, "cp", "-a", proj, work)
    gd = os.path.join(top, "shadow.git")
    sh(top, "git", "init", "-q", "--bare", gd)
    sh(top, "git", f"--git-dir={gd}", "config", "core.bare", "false")
    os.makedirs(os.path.join(gd, "info"), exist_ok=True)
    idx = os.path.join(top, "idx")
    base = ds.snapshot(work, gd, "pre t", idx, "t", ["a.py"])
    t0 = time.time()      # the job starts here: older files are untouched ones
    time.sleep(0.05)
    open(os.path.join(work, "a.py"), "w").write("x = 2\n")
    open(os.path.join(work, "built.py"), "w").write("generated\n")     # ignored: check output
    open(os.path.join(work, "helper.py"), "w").write("stray\n")        # plain file outside --paths
    post = ds.snapshot(work, gd, "post t", idx, "t", ["a.py"])
    job = {"id": "t", "dir": work, "project": proj, "shadow": gd, "base": base, "post": post, "started": t0}
    copied, skipped = ds.copy_back_untracked(job)
    check("ignored output is copied back", os.path.exists(os.path.join(proj, "built.py")))
    check("a plain file outside --paths is not copied", not os.path.exists(os.path.join(proj, "helper.py")))
    check("it is reported as left out", job.get("left_out") == ["helper.py"], str(job.get("left_out")))
    check("ignored files do not count as outside changes",
          ds.outside_changes({}, {"built.py": (1, 1), "helper.py": (1, 1)}, work) == ["created helper.py"])

# LiveMeter: speeds from synthetic event streams (times in ms)
m = ds.LiveMeter()
m.available = True
m.feed("session.step.started", {"started": 1000}, 1000)
m.feed("session.reasoning.delta", {"delta": "x" * 400}, 3000)        # first token after 2 s
m.feed("session.text.delta", {"delta": "y" * 400}, 3500)
m.feed("session.text.delta", {"delta": "y" * 400}, 5000)
check("in-flight tokens are an estimate from characters", m.estimate() == round(1200 / 3.5), str(m.estimate()))
m.feed("session.step.ended", {"tokens": {"output": 200, "reasoning": 100}}, 5100)
r = ds.set_speeds(dict(m.speed))
check("streamed step: decode = 300 tok / 2 s = 150", (r["dec_tok_s"], r["tok_kind"]) == (150, "decode"), str(r))
check("streamed step: throughput = 300 tok / 4 s = 75", r["thr_tok_s"] == 75, str(r))
check("streamed step: time to first token 2.0 s", m.ttft_s() == 2.0, str(m.ttft_s()))
check("finished step count is exact, estimate reset", (m.done_out, m.estimate()) == (300, 0))
check("chars per token calibrated on the finished step", m.cpt == 4.0, str(m.cpt))

h = ds.LiveMeter()                                                    # hidden reasoning (Muse)
h.feed("session.step.started", {"started": 0}, 0)
h.feed("session.text.delta", {"delta": "z" * 300}, 80000)             # 80 s of silent thinking first
h.feed("session.text.delta", {"delta": "z" * 300}, 86000)
h.feed("session.step.ended", {"tokens": {"output": 700, "reasoning": 5750}}, 86100)
r = ds.set_speeds(dict(h.speed))
check("hidden reasoning: no decode figure, no fake first-token time", (r["dec_tok_s"], h.ttft) == (None, []), str(r))
check("hidden reasoning: throughput = 6450 tok / 86 s = 75, shown as such", (r["tok_s"], r["tok_kind"]) == (75, "throughput"), str(r))

t = ds.LiveMeter()
t.available = True
lab = t.feed("session.tool.called", {"id": "c1", "tool": "shell", "input": {"command": "pytest -q"}}, 10_000)
check("tool call gives a label and the tool phase", lab == "shell pytest -q" and t.view(11_000)[1] == "tool", str(lab))
t.feed("session.tool.success", {"id": "c1"}, 12_000)
check("tool finished: back to waiting", t.view(13_000)[1] == "waiting")

y = ds.Tally("ses_a")
for ev in ({"type": "step_start", "sessionID": "ses_b"}, {"type": "step_start"},          # a retried step
           {"type": "tool", "part": {"tool": "shell", "state": {"input": {"command": "ls"}}}},
           {"type": "text", "part": {"messageID": "m1", "text": "STATUS: "}},
           {"type": "text", "part": {"messageID": "m1", "text": "DONE"}},
           {"type": "step_finish", "part": {"tokens": {"input": 10, "output": 5, "reasoning": 7, "cache": {"read": 3}}}},
           {"type": "error", "error": {"message": "boom"}}):
    y.feed(ev)
check("tally: retried step counted once, session id followed", (y.steps, y.sid) == (1, "ses_b"))
check("tally: tokens, text, errors", (y.tok, y.final_text(), y.errors) ==
      ({"input": 10, "output": 12, "cache_read": 3}, "STATUS: DONE", ["boom"]), str((y.tok, y.final_text(), y.errors)))

print("\n%d failed" % len(fails) if fails else "\nall passed")
sys.exit(1 if fails else 0)
