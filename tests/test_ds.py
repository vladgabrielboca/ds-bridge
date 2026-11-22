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

# board data (T1): activity.jsonl, report.txt, job.json round fields, batch.json, pins
R, RES, B = sys.modules["dslib.runner"], sys.modules["dslib.results"], sys.modules["dslib.batch"]
saved = (R.JOBS, RES.JOBS, RES.PINS, B.JOBS, B.DS_BIN)
with tempfile.TemporaryDirectory() as st:
    jobs_dir, pins_dir = os.path.join(st, "jobs"), os.path.join(st, "pins")
    R.JOBS = RES.JOBS = B.JOBS = jobs_dir
    RES.PINS = pins_dir
    try:
        os.makedirs(os.path.join(jobs_dir, "J"))
        job = {"id": "J", "round": 1, "status": "open", "model": "m"}
        stream = [{"type": "step_start"},
                  {"type": "tool", "part": {"tool": "bash", "state": {"input": {"command": "curl -H 'x-api-key: abcdefgh1234' sk-abcdef123456 " + "y" * 300}}}},
                  {"type": "tool", "part": {"tool": "read", "state": {"input": {"filePath": "src/a.py"}}}},
                  {"type": "step_finish", "part": {"tokens": {"input": 100, "output": 10, "reasoning": 5, "cache": {"read": 900}}, "cost": 0.01}},
                  {"type": "step_start"},
                  {"type": "tool", "part": {"tool": "edit", "state": {"input": {"filePath": "src/a.py", "oldString": "SECRET", "newString": "BODY"}}}},
                  {"type": "text", "part": {"messageID": "m", "text": "STATUS: DONE"}},
                  {"type": "step_finish", "part": {"tokens": {"input": 50, "output": 20, "reasoning": 0, "cache": {"read": 1000}}, "cost": 0.02}}]
        y = ds.Tally("s", R.activity_for(job))
        for ev in stream:
            y.feed(ev)
        act_path = os.path.join(jobs_dir, "J", "activity.jsonl")
        recs = [json.loads(l) for l in open(act_path)]
        check("activity: 3 tools + 2 usage records, seq 1..5, round 1",
              [r["kind"] for r in recs] == ["tool", "tool", "usage", "tool", "usage"]
              and [r["seq"] for r in recs] == [1, 2, 3, 4, 5] and all(r["round"] == 1 and r["ts"].endswith("Z") for r in recs), str(recs))
        check("activity usage: in = input + cache_read of the step, out cumulative, cache of the step",
              [(r["in"], r["out"], r["cache"]) for r in recs if r["kind"] == "usage"] == [(1000, 15, 900), (1050, 35, 1000)], str(recs))
        a0 = recs[0]["arg"]
        check("activity arg: keys masked, clamped to 200", "abcdef123456" not in a0 and "abcdefgh1234" not in a0
              and "sk-[REDACTED]" in a0 and len(a0) == 200, a0)
        check("activity arg: file-writing tool shows the path only", (recs[3]["tool"], recs[3]["arg"]) == ("edit", "src/a.py")
              and "SECRET" not in open(act_path).read(), str(recs[3]))
        check("tally last keeps its old shape", y.last == "edit src/a.py", y.last)
        job2 = {"id": "J", "round": 2}                                    # a revise round: new process, same file
        R.activity_for(job2).tool("glob", {"pattern": "*.py"})
        last = json.loads(open(act_path).read().splitlines()[-1])
        check("activity: a new round appends, seq continues", (last["seq"], last["round"], last["arg"]) == (6, 2, "*.py"), str(last))
        # live.txt (T7): deltas stream in, a message with deltas is not written again from stdout,
        # one without deltas is, a new round gets a header, and the cap keeps the tail
        lj = {"id": "J", "round": 1}
        lv = R.live_for(lj)
        lv.delta("m1", "Hello ")
        lv.delta("m1", "world")
        lv.whole("m1", "Hello world")
        lv.whole("m2", "second message")
        live_path = os.path.join(jobs_dir, "J", "live.txt")
        check("live: deltas, then a whole message with a blank line between, no duplicate",
              open(live_path).read() == "Hello world\n\nsecond message", repr(open(live_path).read()))
        lj2 = {"id": "J", "round": 2}
        R.live_for(lj2).delta("m3", "round two")
        check("live: a new round appends after a header",
              open(live_path).read().endswith("second message\n\n── round 2 ──\n\nround two"), repr(open(live_path).read()[-60:]))
        lv2 = lj2["_live"]
        line = "é" * 40 + "\n"                                           # 81 bytes, 2-byte chars
        for i in range(4000):
            lv2.delta("m4", line)
        lv2.delta("m4", "THE END")
        body = open(live_path, "rb").read()
        check("live: capped at 256 KB, tail kept, starts on a whole line",
              len(body) <= R.LIVE_CAP and body.endswith(b"THE END") and body.startswith("é".encode())
              and lv2.size == len(body) and b"Hello" not in body, str(len(body)))
        y2 = ds.Tally("s", None, R.live_for({"id": "J", "round": 2}))
        y2.feed({"type": "text", "part": {"messageID": "m5", "text": "from stdout"}})
        check("live: Tally writes a stdout text part when no deltas came", open(live_path).read().endswith("\n\nfrom stdout"))
        RES.write_report(job, "STATUS: DONE\nSUMMARY: x")
        check("report.txt holds the final text", open(os.path.join(jobs_dir, "J", "report.txt")).read() == "STATUS: DONE\nSUMMARY: x")
        res = {"tokens": {"input": 150, "output": 35, "cache_read": 1900}, "cost": 0.03, "tools": 3}
        job["_prog"] = {"steps": 2, "tools": 3}
        RES.end_round(job, res, "OK")
        job["round"], job["_prog"] = 2, {"steps": 1, "tools": 1}
        RES.end_round(job, dict(res, cost=0.01), "FAIL")
        jj = json.load(open(os.path.join(jobs_dir, "J", "job.json")))
        check("job.json round fields summed over rounds, old fields kept",
              (jj["steps"], jj["tools"], jj["cost"], jj["tokens"], jj["result"], jj["status"], jj["model"]) ==
              (3, 4, 0.04, {"in": 300, "out": 70, "cache": 3800}, "FAIL", "open", "m") and jj["ended"] > 0
              and "_prog" not in jj, str(jj))
        # pins: a pinned landed job is left alone by gc_landed
        for jid in ("P", "U"):
            os.makedirs(os.path.join(jobs_dir, jid))
            json.dump({"id": jid, "status": "landed", "landed_at": 1}, open(os.path.join(jobs_dir, jid, "job.json"), "w"))
        os.makedirs(pins_dir)
        open(os.path.join(pins_dir, "P"), "w").close()
        RES.gc_landed()
        check("gc_landed skips a pinned job, cleans an unpinned one",
              [json.load(open(os.path.join(jobs_dir, j, "job.json")))["status"] for j in ("P", "U")] == ["landed", "approved"])
        # batch.json from cmd_batch, with a fake ds that prints a landed OK line per group
        fake = os.path.join(st, "fake-ds")
        open(fake, "w").write("import os, sys, json\nsys.stdin.read()\n"
                              "g = os.environ.get('DS_BATCH_GROUP', '')\n"
                              "b = json.load(open(os.path.join(%r, os.environ['DS_BATCH'], 'batch.json')))\n"
                              "assert b['groups'][int(g)]['status'] == 'running'\n"
                              "print('OK 1122-12000' + g + ' 3s landed')\n" % jobs_dir)
        B.DS_BIN = fake
        planf = os.path.join(st, "plan.md")
        open(planf, "w").write("# T\nctx\n\n## a first\nfiles: a.py\ndo: x\n\n## b second\nfiles: b.py\ndo: y\nneeds: a\n")
        env_sid = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        import argparse, contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            B.cmd_batch(argparse.Namespace(C=st, plan=planf, steps=None, parallel=2, m=None, check=None,
                                           source=None, brief=None, timeout=None))
        if env_sid is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = env_sid
        bid = buf.getvalue().split()[1]
        bj = json.load(open(os.path.join(jobs_dir, bid, "batch.json")))
        check("batch.json shape for a 2-group plan",
              (bj["id"], bj["title"], bj["plan"], bj["project"], bj["check"], bj["integration"]) == (bid, "T", planf, st, None, "none")
              and bj["ended"] >= bj["started"] > 0
              and bj["groups"] == [{"label": "a", "steps": ["a"], "needs": [], "job": "1122-120000", "status": "OK"},
                                   {"label": "b", "steps": ["b"], "needs": [0], "job": "1122-120001", "status": "OK"}],
              json.dumps(bj))
    finally:
        R.JOBS, RES.JOBS, RES.PINS, B.JOBS, B.DS_BIN = saved

# board actions (T5): ds forget, and a batch stopped by SIGTERM. Both run ds in a subprocess with
# HOME in a temp dir, so STATE is there.
with tempfile.TemporaryDirectory() as h:
    h = os.path.realpath(h)
    S = os.path.join(h, ".local/share/ds-bridge")
    env = dict(os.environ, HOME=h, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    def mkjob(jid, **kw):
        os.makedirs(os.path.join(S, "jobs", jid), exist_ok=True)
        json.dump({"id": jid, "round": 1, **kw}, open(os.path.join(S, "jobs", jid, "job.json"), "w"))

    def forget(*ids):
        r = subprocess.run([sys.executable, os.path.join(HERE, "ds"), "forget", *ids],
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout.strip().splitlines()

    gd = os.path.join(S, "shadow", "x")
    subprocess.run(["git", "init", "-q", "--bare", gd], check=True)
    tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    commit = subprocess.run(["git", f"--git-dir={gd}", "commit-tree", tree, "-m", "s"], env=env,
                            capture_output=True, text=True, check=True).stdout.strip()
    for jid in ("1122-100001", "1122-100009"):
        subprocess.run(["git", f"--git-dir={gd}", "update-ref", f"refs/ds/{jid}/base", commit], check=True)
    copy = os.path.join(S, "work", "1122-100001")
    os.makedirs(copy)
    mkjob("1122-100001", status="landed", project=os.path.join(h, "p"), dir=copy, shadow=gd)
    open(os.path.join(S, "jobs", "1122-100001", "index"), "w").close()
    for sub in ("progress", "pins"):
        os.makedirs(os.path.join(S, sub), exist_ok=True)
    json.dump({"state": "done"}, open(os.path.join(S, "progress", "1122-100001.json"), "w"))
    open(os.path.join(S, "pins", "1122-100001"), "w").close()
    code, out = forget("1122-100001")
    refs = subprocess.run(["git", f"--git-dir={gd}", "for-each-ref", "--format=%(refname)"],
                          capture_output=True, text=True).stdout.split()
    check("forget: a landed job goes completely (dir, copy, refs, progress, pin), other refs stay",
          (code, out) == (0, ["forgot 1122-100001"]) and refs == ["refs/ds/1122-100009/base"]
          and not any(os.path.exists(p) for p in (os.path.join(S, "jobs", "1122-100001"), copy,
                      os.path.join(S, "progress", "1122-100001.json"), os.path.join(S, "pins", "1122-100001"))),
          f"{code} {out} {refs}")
    mkjob("1122-100002", status="open", dir=os.path.join(h, "p"))                    # in place
    os.makedirs(os.path.join(S, "work", "1122-100003"))
    mkjob("1122-100003", status="open", project=os.path.join(h, "p"), dir=os.path.join(S, "work", "1122-100003"))
    mkjob("1122-100004", status="approved")
    os.makedirs(os.path.join(S, "running"))
    json.dump({"pid": os.getpid()}, open(os.path.join(S, "running", "1122-100004.json"), "w"))
    mkjob("1122-100005", status="open", project=os.path.join(h, "p"), dir=os.path.join(S, "work", "gone"))
    code, out = forget("1122-100002", "1122-100003", "1122-100004", "1122-100005", "1122-999999", "../x")
    check("forget: open, running and unknown ids are kept with one line each; an open job whose copy is gone goes",
          code == 1 and len(out) == 6 and out[0].startswith("keep 1122-100002: open, its changes are in")
          and out[1].startswith("keep 1122-100003: open, its copy") and "ds reject" in out[1]
          and out[2] == f"keep 1122-100004: running (pid {os.getpid()})" and out[3] == "forgot 1122-100005"
          and out[4] == "keep 1122-999999: no such job" and out[5] == "keep ../x: no such job"
          and os.path.isdir(os.path.join(S, "jobs", "1122-100003")), str(out))
    bdir = os.path.join(S, "jobs", "b1122-110000")
    os.makedirs(bdir)
    json.dump({"id": "b1122-110000", "ended": 5, "pid": os.getpid(),
               "groups": [{"job": "1122-110001"}, {"job": None}], "fix_job": "1122-110003"},
              open(os.path.join(bdir, "batch.json"), "w"))
    mkjob("1122-110001", status="approved", batch="b1122-110000")
    mkjob("1122-110002", status="open", batch="b1122-110000", dir=os.path.join(h, "p"))   # found by its job.json
    mkjob("1122-110003", status="landed", batch="b1122-110000")
    code, out = forget("b1122-110000")
    check("forget: a batch with an open job is kept, nothing removed",
          code == 1 and out[0].startswith("keep b1122-110000: job 1122-110002 is open")
          and os.path.isdir(os.path.join(S, "jobs", "1122-110001")), str(out))
    mkjob("1122-110002", status="rejected", batch="b1122-110000")
    open(os.path.join(S, "pins", "b1122-110000"), "w").close()
    code, out = forget("b1122-110000")
    check("forget: a closed batch goes with its 3 jobs and its pin",
          (code, out) == (0, ["forgot b1122-110000 and its 3 jobs"]) and not any(
              os.path.exists(os.path.join(S, x)) for x in ("jobs/b1122-110000", "jobs/1122-110001",
                                                           "jobs/1122-110002", "jobs/1122-110003", "pins/b1122-110000")),
          str(out))

    # a batch stopped with SIGTERM: the running child gets SIGTERM, nothing new starts, batch.json ends
    fake = os.path.join(h, "fake-ds")
    open(fake, "w").write("import os, signal, sys, time\nsys.stdin.read()\n"
                          "def term(*_):\n    open(%r, 'w').close()\n    sys.exit(143)\n"
                          "signal.signal(signal.SIGTERM, term)\ntime.sleep(30)\n" % os.path.join(h, "got-term"))
    planf = os.path.join(h, "plan.md")
    open(planf, "w").write("# T\n\n## a first\nfiles: a.py\ndo: x\n\n## b second\nfiles: b.py\ndo: y\nneeds: a\n")
    runner = ("import sys, argparse; sys.path.insert(0, %r); import dslib.cli as ds; import dslib.batch as B\n"
              "B.DS_BIN = %r; ds.install_signal_handlers()\n"
              "B.cmd_batch(argparse.Namespace(C=%r, plan=%r, steps=None, parallel=2, m=None, check=None,"
              " source=None, brief=None, timeout=None))\n" % (HERE, fake, h, planf))
    bp = subprocess.Popen([sys.executable, "-c", runner], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    bj, t0 = {}, time.time()
    while time.time() - t0 < 10:
        for n in os.listdir(os.path.join(S, "jobs")):
            if n.startswith("b1") and n != "b1122-110000":
                try:
                    bj = json.load(open(os.path.join(S, "jobs", n, "batch.json")))
                except (OSError, ValueError):
                    pass
        if (bj.get("groups") or [{}])[0].get("status") == "running":
            break
        time.sleep(0.1)
    time.sleep(0.5)       # the fake child sets its handler
    bp.terminate()
    try:
        bp.wait(timeout=20)
    except subprocess.TimeoutExpired:
        bp.kill()
    try:
        bj = json.load(open(os.path.join(S, "jobs", bj.get("id", "?"), "batch.json")))
    except (OSError, ValueError):
        pass
    check("batch stop: pid recorded, child got SIGTERM, every group STOPPED, ended, stopped",
          bj.get("pid") == bp.pid and os.path.exists(os.path.join(h, "got-term"))
          and [g_.get("status") for g_ in bj.get("groups") or []] == ["STOPPED", "STOPPED"]
          and bj.get("ended") and bj.get("stopped") is True and bp.returncode == 128 + 15,
          json.dumps(bj) + f" rc={bp.returncode} err={bp.stderr.read().decode()[-300:]}")

print("\n%d failed" % len(fails) if fails else "\nall passed")
sys.exit(1 if fails else 0)
