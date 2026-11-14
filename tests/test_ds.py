#!/usr/bin/env python3
"""Checks for ds that need no worker and no network: python3 tests/test_ds.py"""
import json, os, subprocess, sys, tempfile, time

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

# OpenCode >= 2.3: first-token + streamed server times, same numbers as upstream session-turn.test.ts
o = ds.LiveMeter()
o.available = True
o.feed("session.step.started.1", {"started": 6000}, 6000)
o.feed("session.step.first-token.1", {}, 8000)
o.feed("session.text.delta", {"delta": "a" * 40}, 9000)
check("first-token mode: no live number before a step ends", o.view(9100)[0] is None, str(o.view(9100)))
o.feed("session.step.streamed", {}, 10000)
o.feed("session.step.ended", {"tokens": {"output": 20, "reasoning": 5}}, 20000)
check("first-token mode: live number = last step's exact rate (25 tok / 2 s = 12.5)", o.last_step_tok_s == 12, str(o.last_step_tok_s))
o.feed("session.step.started", {"started": 24000}, 24000)
o.feed("session.step.first-token", {}, 25000)
o.feed("session.step.first-token", {}, 26000)                          # only the first one counts
o.feed("session.step.streamed", {}, 28000)
o.feed("session.step.ended", {"tokens": {"output": 30, "reasoning": 10}}, 31000)
r = ds.set_speeds(dict(o.speed))
check("first-token mode: 65 tok / 5 s = 13 tok/s, from opencode", (r["tok_s"], r["tok_kind"], r["tok_src"]) == (13, "decode", "opencode"), str(r))
check("first-token mode: ttft from step start (2 s, 1 s)", o.ttft == [2000, 1000], str(o.ttft))

mh = ds.LiveMeter()                                                    # Muse: hidden reasoning, but the server saw reasoning start
mh.feed("session.step.started", {"started": 0}, 0)
mh.feed("session.step.first-token", {}, 1500)
mh.feed("session.text.delta", {"delta": "z" * 300}, 80000)
mh.feed("session.step.streamed", {}, 86000)
mh.feed("session.step.ended", {"tokens": {"output": 700, "reasoning": 5750}}, 86100)
r = ds.set_speeds(dict(mh.speed))
check("first-token mode, hidden reasoning: decode 6450 tok / 84.5 s = 76", (r["tok_s"], r["tok_kind"]) == (76, "decode"), str(r))

mx = ds.LiveMeter()                                                    # one step without first-token (upgrade mid-job)
mx.feed("session.step.started", {"started": 0}, 0)
mx.feed("session.text.delta", {"delta": "q" * 100}, 1000)
mx.feed("session.text.delta", {"delta": "q" * 100}, 3000)
mx.feed("session.step.ended", {"tokens": {"output": 100, "reasoning": 0}}, 3100)
mx.feed("session.step.started", {"started": 5000}, 5000)
mx.feed("session.step.first-token", {}, 6000)
mx.feed("session.step.streamed", {}, 8000)
mx.feed("session.step.ended", {"tokens": {"output": 100, "reasoning": 0}}, 8100)
r = ds.set_speeds(dict(mx.speed))
check("mixed steps are not labelled opencode", r["tok_src"] == "sse", str(r))

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

# ds cost --detail: per-item cost split by size, cache rebuild detection
calls = [{"cr": 0, "cw": 1000, "inp": 0, "out": 100, "ts": 0, "made": ["Bash: cat a", "Read: f.png"],
          "items": [("shell output", "Bash: cat a", 300), ("images", "Read: f.png", 100)]},
         {"cr": 1000, "cw": 3000, "inp": 0, "out": 10, "ts": 60, "made": [], "items": []},
         {"cr": 0, "cw": 4200, "inp": 0, "out": 10, "ts": 4000, "made": [], "items": []},
         {"cr": 4200, "cw": 0, "inp": 0, "out": 10, "ts": 4010, "made": [], "items": []}]
ctx = [c["cr"] + c["cw"] + c["inp"] for c in calls]
it = {lab: (tok, c) for _, lab, tok, c in ds.item_costs(calls, ctx)}
check("item cost: growth minus own output split 3:1 by size", round(it["call 1: Bash: cat a"][0]) == 2175
      and round(it["call 1: Read: f.png"][0]) == 725, str(it))
check("item cost: re-read by the 3 later calls (0.1 x 2175 x 3)", it["call 1: Bash: cat a"][1] == 652.5
      and it["call 1 Claude wrote: Bash: cat a, Read: f.png"][0] == 100, str(it))
rb = ds.cache_rebuilds(calls, ctx)
check("cache rebuild: only after the 66 min gap, not on growth", [(r[0], round(r[1]), r[2]) for r in rb] == [(3, 3940, 4000)], str(rb))

# hooks: handoff adoption, context nudge, read gate (own HOME, nothing real touched)
os.makedirs(os.path.expanduser("~/.cache"), exist_ok=True)
with tempfile.TemporaryDirectory(dir=os.path.expanduser("~/.cache")) as h:   # not /tmp: the read gate exempts it
    env = dict(os.environ, HOME=h)
    hook = lambda name, payload: subprocess.run([os.path.join(HERE, "hooks", name)], input=json.dumps(payload),
                                                capture_output=True, text=True, env=env).stdout
    os.makedirs(os.path.join(h, ".claude/projects/p"))
    os.makedirs(os.path.join(h, ".local/state/ds-bridge/delegate"))
    os.makedirs(os.path.join(h, ".local/share/ds-bridge/jobs/J1"))
    json.dump({"owner": "old", "status": "open"}, open(os.path.join(h, ".local/share/ds-bridge/jobs/J1/job.json"), "w"))
    json.dump({"model": "muse", "shown": True}, open(os.path.join(h, ".local/state/ds-bridge/delegate/old"), "w"))
    open(os.path.join(h, ".claude/projects/p/old.jsonl"), "w").close()
    proj2 = os.path.join(h, "proj")
    r = subprocess.run([os.path.join(HERE, "ds"), "handoff", "-C", proj2, "-"], input="Goal: test the handoff.\nNext: step two of the plan.\n",
                       capture_output=True, text=True, env=dict(env, CLAUDE_CODE_SESSION_ID="old"))
    notes = open(os.path.join(proj2, ".plan/HANDOFF.md")).read() if os.path.exists(os.path.join(proj2, ".plan/HANDOFF.md")) else ""
    check("handoff writes notes + open job + delegation state", r.returncode == 0 and "step two" in notes
          and "job J1 open" in notes and "on, model muse" in notes, r.stderr + notes)
    o = hook("delegate.py", {"hook_event_name": "SessionStart", "source": "clear", "session_id": "x",
                             "transcript_path": os.path.join(h, ".claude/projects/other/x.jsonl")})
    check("handoff ignored by a chat of another project", o.strip() == "", o)
    o = hook("delegate.py", {"hook_event_name": "SessionStart", "source": "clear", "session_id": "new",
                             "transcript_path": os.path.join(h, ".claude/projects/p/new.jsonl")})
    st = json.load(open(os.path.join(h, ".local/state/ds-bridge/delegate/new")))
    job = json.load(open(os.path.join(h, ".local/share/ds-bridge/jobs/J1/job.json")))
    check("handoff adopted after /clear: notes, delegation, job owner", "step two" in o and st.get("model") == "muse"
          and job["owner"] == "new" and not os.path.exists(os.path.join(h, ".local/state/ds-bridge/handoff/pending.json")), o[:200])
    tr = os.path.join(h, ".claude/projects/p/new.jsonl")
    seen = []
    for n in (100_000, 160_000, 170_000, 240_000, 90_000, 200_000):
        with open(tr, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": n}}}) + "\n")
        o = hook("delegate.py", {"hook_event_name": "UserPromptSubmit", "session_id": "new", "transcript_path": tr})
        seen.append("ds handoff" in o)
    check("nudge at 150k, again 75k later, again after compaction", seen == [False, True, False, True, False, True], str(seen))
    big = os.path.join(h, "proj", "big.py")
    open(big, "w").write("x = 1\n" * 900)
    pre = {"hook_event_name": "PreToolUse", "session_id": "new", "prompt_id": "p1", "cwd": proj2,
           "tool_name": "Read", "tool_input": {"file_path": big}}
    a1, a2 = hook("discipline.py", pre), hook("discipline.py", pre)
    rng = hook("discipline.py", dict(pre, tool_input={"file_path": big, "offset": 1, "limit": 80}))
    cat = hook("discipline.py", dict(pre, tool_name="Bash", tool_input={"command": f"cd {proj2} && cat big.py"}))
    pipe = hook("discipline.py", dict(pre, tool_name="Bash", tool_input={"command": f"cat {big} | head"}))
    check("read gate: big Read denied once, then passes; ranged and piped pass; cat counted",
          '"deny"' in a1 and not a2.strip() and not rng.strip() and '"deny"' in cat and not pipe.strip(),
          str([a1[:60], a2, rng, cat[:60], pipe]))

print("\n%d failed" % len(fails) if fails else "\nall passed")
sys.exit(1 if fails else 0)
