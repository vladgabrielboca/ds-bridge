#!/usr/bin/env python3
"""Claude Code hook: enforce the rules that save the most Claude usage around ds results.

Adapted from cheap-agent's hook (src/hook.ts, MIT), only for chats with delegation on
(`ds delegate on`):
- PostToolUse (Bash): a ds result block is recorded for this user prompt: OK with a passed check
  ("ok"), or not finished ("notdone": FAIL / PARTIAL / CONFLICT / BLOCKED).
  ds also writes each finished job's or batch's outcome to <state>/<session>.result for the chat
  that started it; the next PreToolUse adopts it. That covers results that arrive as a background
  notice (`ds wait --until-done`), which no PostToolUse ever sees. A new prompt typed by the user
  drops an unadopted result; a task notification does not.
- PreToolUse (any tool), same prompt only:
  * after "ok": running tests again is denied (the check already passed in the worker's copy);
  * after "notdone": only reads, ds commands, asking the user (AskUserQuestion) and loading tools
    pass; anything else is denied until the user has been asked, then the state clears
    (a FAIL with check=env, where the check could not run at all, is not "notdone").
- PreToolUse (Bash), always: a `ds wait` must be `--until-done` in a background Bash call (one
  notice when the job ends instead of a Claude turn per 9-minute poll). Commands that write project files (redirects, tee, sed -i,
  scripts opening files for writing) are denied (cp/mv are allowed: no content goes through Claude); /tmp, ~/.claude, ds state and .plan/
  stay free, and a ds brief inside a heredoc is ignored.
- PreToolUse (Read / Bash cat), always: a whole-file read of more than read_gate_lines (400) lines
  is denied once with cheaper ways to get the facts (grep, a ranged Read, ds research); the same call
  again passes. Skills, memory, /tmp and .plan/ files are exempt.
A new user prompt starts fresh (state is keyed by Claude Code's prompt_id, or by our own counter
that the UserPromptSubmit event advances when prompt_id is missing). Never breaks the session: any error
allows the call.
"""
import json
import os
import re
import sys

DELEGATE = os.path.expanduser("~/.local/state/ds-bridge/delegate")
STATE = os.path.expanduser("~/.local/state/ds-bridge/discipline")
DS_CMD = re.compile(r"(?:^|[\s;&|/])ds\s+(run|revise|reply|wait|batch)\b")
DS_ANY = re.compile(r"(?:^|[\s;&|/])ds\s+(revise|reply|reject|research|diff|log|wait|list|approve|rebase|stats)\b")
TEST_CMD = re.compile(r"\bpytest\b|runtests\.py|\b(?:npm|pnpm|yarn|bun) (?:run )?test\b|node --test|\btox\b"
                      r"|\bvitest\b|\bjest\b|\bgo test\b|\bcargo test\b|\b(?:mvn|gradle|gradlew) test\b"
                      r"|\brspec\b|\bphpunit\b|-m unittest\b|\bmake test\b|render\.sh --check")
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "ToolSearch", "AskUserQuestion", "TaskStop"}
READ_BASH = re.compile(r"^\s*(git (diff|log|show|status)|cat|head|tail|ls|grep|rg|wc|find|pwd|echo)\b")
NOT_DONE = re.compile(r"^(FAIL|PARTIAL|CONFLICT|BLOCKED)\s+\S+", re.M)
OK_PASSED = re.compile(r"^OK\s+\S+.*\bcheck=pass\b", re.M)
ENV_FAIL = re.compile(r"^FAIL\s+\S+.*\bcheck=env\b", re.M)
# Bash forms that write a file (cheap-agent's hook: the benchmark showed Claude writing whole files
# this way to get past the edit guard). Heredoc bodies are stripped first: ds briefs travel in them.
BASH_WRITES = [re.compile(r"(?:^|[^<>0-9&])>{1,2}\s*[\"']?(?P<path>[^\s\"'|;&()]+)"),
               re.compile(r"\btee\s+(?:-a\s+)?[\"']?(?P<path>[^\s\"'|;&]+)")]
# cp/mv stay allowed: copying existing files puts no file content through Claude (unlike cheap-agent)
BASH_INPLACE = re.compile(r"\b(?:sed|perl)\s+(?:-[a-zA-Z]*i|--in-place)\b")
BASH_CODE_WRITES = re.compile(r"\b(?:python3?|node)\b[\s\S]*(?:open\([^)]*[\"'][wa]|writeFile|\.write_text|\.write_bytes)")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?:\n|$)", re.S)
FREE_PREFIXES = ("/tmp/", "/dev/", os.path.expanduser("~/.claude/"), os.path.expanduser("~/.local/share/ds-bridge/"),
                 os.path.expanduser("~/.local/state/ds-bridge/"))
DS_WAIT = re.compile(r"(?:^|[\s;&|/])ds\s+wait\b")
BATCH_OK = re.compile(r"^BATCH\s+\S+.*\bnot_ok=0\b.*\bintegration=(none|pass)", re.M)


CONF = os.path.expanduser("~/.config/ds-bridge/config.json")


def takeover():
    """ds config "takeover": "ask" (default) or "sonnet"."""
    try:
        with open(CONF) as f:
            v = json.load(f).get("takeover")
        return v if v in ("ask", "sonnet") else "ask"
    except (OSError, ValueError, AttributeError):
        return "ask"


def on(sid):
    return bool(sid) and os.path.exists(os.path.join(DELEGATE, sid))


def state_path(sid):
    return os.path.join(STATE, re.sub(r"[^\w-]", "_", sid) + ".json")


def load(sid):
    try:
        with open(state_path(sid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(sid, st):
    os.makedirs(STATE, exist_ok=True)
    tmp = state_path(sid) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, state_path(sid))


def text_of(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(text_of(x) for x in v)
    if isinstance(v, dict):
        for k in ("stdout", "output", "text", "content"):
            if k in v:
                return text_of(v[k])
    return ""


def deny(reason):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}) + "\n")


def prompt_of(p):
    """Claude Code's prompt_id, or our own per-session prompt counter (UserPromptSubmit) as fallback."""
    if p.get("prompt_id"):
        return p["prompt_id"]
    sid = p.get("session_id") or ""
    try:
        with open(state_path(sid) + ".seq") as f:
            return "seq-" + f.read().strip()
    except OSError:
        return None


def result_path(sid):
    return state_path(sid) + ".result"


def adopt_result(sid, prompt):
    """A result ds recorded for this chat becomes the state of the current prompt."""
    try:
        with open(result_path(sid)) as f:
            r = json.load(f)
        os.remove(result_path(sid))
    except (OSError, ValueError):
        return
    if isinstance(r, dict) and r.get("state") in ("ok", "notdone"):
        save(sid, {"prompt": prompt, "state": r["state"]})


def new_prompt(p):
    sid = p.get("session_id")
    if not on(sid):
        return
    # the user typed something: an older result no longer restricts this turn. A background task's
    # completion notice is not the user speaking, so its result stays to be adopted.
    if "<task-notification>" not in str(p.get("prompt") or ""):
        try:
            os.remove(result_path(sid))
        except OSError:
            pass
    os.makedirs(STATE, exist_ok=True)
    try:
        n = int(open(state_path(sid) + ".seq").read().strip() or 0) + 1
    except (OSError, ValueError):
        n = 1
    with open(state_path(sid) + ".seq", "w") as f:
        f.write(str(n))


def post(p):
    sid, prompt = p.get("session_id"), prompt_of(p)
    if p.get("tool_name") != "Bash" or not on(sid) or not prompt:
        return
    cmd = (p.get("tool_input") or {}).get("command") or ""
    if not DS_CMD.search(cmd):
        return
    out = text_of(p.get("tool_response"))
    # a check that could not run at all (check=env) is not the worker failing: nothing to take over
    if NOT_DONE.search(out) and not ENV_FAIL.search(out):
        save(sid, {"prompt": prompt, "state": "notdone"})
    elif OK_PASSED.search(out) or BATCH_OK.search(out):
        save(sid, {"prompt": prompt, "state": "ok"})


def bash_write(cmd, cwd):
    """The file a Bash command writes outside scratch space, or a description of an in-place edit."""
    body = HEREDOC.sub("\n", cmd)
    plan_heredoc = re.search(r"<<", cmd) and re.search(r"(?:^|[\s;&|/])ds\s+(run|revise|batch|research)\b", cmd)
    # resolve each part against the folder the command is in at that point (`cd X && …`)
    targets, cur = [], cwd or os.getcwd()
    for part in re.split(r"&&|\|\||;|\n", body):
        cd = re.match(r"\s*(?:pushd|cd)\s+(?P<d>[^\s;&|]+)\s*$", part)
        if cd:
            cur = os.path.join(cur, os.path.expanduser(cd.group("d").strip("\"'")))
            continue
        targets += [(m.group("path"), cur) for rx in BASH_WRITES for m in rx.finditer(part)]
    for t, base in targets:
        if t.startswith("&") or t in ("/dev/null", "-"):
            continue
        full = os.path.realpath(os.path.join(base, os.path.expanduser(t)))
        if not full.startswith(FREE_PREFIXES) and "/.plan/" not in full:   # ds batch plans may live in .plan/
            return full
    if BASH_INPLACE.search(body):
        return "an in-place sed/perl edit"
    if BASH_CODE_WRITES.search(cmd if not plan_heredoc else body):
        # code that writes files: allowed only when every quoted path in it is scratch space
        quoted = re.findall(r"[\"']((?:/|~/)[^\"'\n]+)[\"']", cmd)
        if not quoted or any(not os.path.realpath(os.path.expanduser(q)).startswith(FREE_PREFIXES) for q in quoted):
            return "a script that writes files"
    return None


READ_GATE_FREE = (os.path.expanduser("~/.claude/"), "/tmp/", "/dev/")


def conf_int(key, default):
    try:
        with open(CONF) as f:
            v = json.load(f).get(key)
        return default if v is None else int(v)
    except (OSError, ValueError, AttributeError, TypeError):
        return default


def count_lines(path, cap=100000):
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in zip(range(cap), f))
    except OSError:
        return 0


def big_read(tool, ti, cwd):
    """(lines, files) of a whole-file read: the Read tool without a small range, or plain `cat`
    of files (not piped into a filter). Skills, memory, scratch and .plan/ files are not counted."""
    files, lines = [], 0
    def add(path, n=None):
        nonlocal lines
        full = os.path.realpath(path)
        if full.startswith(READ_GATE_FREE) or "/.plan/" in full or not os.path.isfile(full):
            return
        k = count_lines(full)
        k = min(k, n) if n else k
        if k:
            files.append(full)
            lines += k
    if tool == "Read":
        if ti.get("offset") and ti.get("limit"):
            return 0, []
        fp = ti.get("file_path") or ""
        if fp.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ipynb")):
            return 0, []
        add(fp, ti.get("limit"))
    elif tool == "Bash":
        cur = cwd or os.getcwd()
        for part in re.split(r"&&|\|\||;|\n", HEREDOC.sub("\n", ti.get("command") or "")):
            cd = re.match(r"\s*(?:pushd|cd)\s+(?P<d>[^\s;&|]+)\s*$", part)
            if cd:
                cur = os.path.join(cur, os.path.expanduser(cd.group("d").strip("\"'")))
                continue
            m = re.match(r"\s*cat\s+([^|<>]+)$", part)
            if m:
                for a in m.group(1).split():
                    if not a.startswith("-"):
                        add(os.path.join(cur, os.path.expanduser(a.strip("\"'"))))
    return lines, files


def read_gate(p, sid):
    """Deny a big whole-file read once: it stays in the context and is re-read on every later
    call. The same call again passes (Claude decided it needs all of it)."""
    limit = conf_int("read_gate_lines", 400)
    tool = p.get("tool_name") or ""
    if limit <= 0 or tool not in ("Read", "Bash"):
        return False
    ti = p.get("tool_input") or {}
    lines, files = big_read(tool, ti, p.get("cwd"))
    if lines <= limit:
        return False
    key = json.dumps([tool, ti.get("file_path"), ti.get("command"), ti.get("offset"), ti.get("limit")])
    path = state_path(sid) + ".reads"
    try:
        seen = json.load(open(path))
    except (OSError, ValueError):
        seen = []
    if key in seen:
        return False
    os.makedirs(STATE, exist_ok=True)
    with open(path, "w") as f:
        json.dump((seen + [key])[-50:], f)
    deny(f"This reads {lines} whole lines ({len(files)} file(s), roughly {lines * 12 // 1000 or 1}k tokens) that stay "
         "in the context and are paid again on every later call. Read only what you need: grep or a ranged "
         "Read (offset/limit), or ask `ds research -C <dir> \"<question>\"` (the worker reads, you get a short "
         "answer). If you really need all of it, send the same call again and it passes.")
    return True


def pre(p):
    sid, prompt = p.get("session_id"), prompt_of(p)
    if p.get("agent_id") or not on(sid) or not prompt:
        return
    tool = p.get("tool_name") or ""
    cmd = (p.get("tool_input") or {}).get("command") or "" if tool == "Bash" else ""
    if read_gate(p, sid):
        return
    if tool == "Bash" and cmd:
        what = bash_write(cmd, p.get("cwd"))
        if what:
            deny(f"Bash writing {what}: that is editing a project file, which the edit guard covers "
                 "(small edits with Edit/Write are fine). Delegate bigger changes with ds run; "
                 "scratch files belong in /tmp.")
            return
    if tool == "Bash" and DS_WAIT.search(cmd):
        bg = bool((p.get("tool_input") or {}).get("run_in_background"))
        if not bg or not re.search(r"--until-done\b", cmd):
            deny("Waiting on a ds job costs a full Claude turn per check. Run it once as "
                 "`ds wait <job> --until-done` with Bash run_in_background: true, then end your turn: "
                 "you get one notice with the result when it ends.")
            return
    adopt_result(sid, prompt)
    st = load(sid)
    if st.get("prompt") != prompt:
        return
    if st.get("state") == "ok":
        if tool == "Bash" and TEST_CMD.search(cmd) and not DS_ANY.search(cmd) and not DS_CMD.search(cmd):
            deny("ds already ran this check and it passed in the worker's copy: don't re-run tests. "
                 "Report the result, or start the next step.")
        return
    if st.get("state") == "notdone":
        mode = takeover()
        if tool == "AskUserQuestion" or (mode == "sonnet" and tool in ("Agent", "Task")):
            save(sid, {})   # the user is asked, or the step is handed to Sonnet: the guard is done
            return
        if mode == "sonnet" and tool in READ_TOOLS or mode == "sonnet" and tool == "Bash" and (
                DS_ANY.search(cmd) or DS_CMD.search(cmd) or READ_BASH.match(cmd)):
            return
        if mode == "sonnet":
            deny("The worker did not finish this step: hand it to your Agent tool with model sonnet, "
                 "working in the job's directory from the ds block (the task, the user's words, found:, "
                 "error: and the check), then ds approve the job and report its answer. "
                 "Don't do it yourself. (The user can set \"takeover\": \"ask\" in the ds config.)")
            return
        if tool in READ_TOOLS:
            return
        if tool == "Bash" and (DS_ANY.search(cmd) or DS_CMD.search(cmd) or READ_BASH.match(cmd)):
            return
        deny("The worker did not finish this step: ask the user first with one question (ds revise "
             "with the findings; you finish it yourself; or stop), "
             "then do what they pick. Don't take it over yourself.")


def main():
    try:
        p = json.loads(sys.stdin.read())
        if not isinstance(p, dict):
            return 0
        ev = p.get("hook_event_name")
        if ev == "PostToolUse":
            post(p)
        elif ev == "PreToolUse":
            pre(p)
        elif ev == "UserPromptSubmit":
            new_prompt(p)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
