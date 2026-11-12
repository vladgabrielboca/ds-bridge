#!/usr/bin/env python3
"""Claude Code hook: enforce the two rules that save the most Claude usage after a ds result.

Adapted from cheap-agent's hook (src/hook.ts, MIT), only for chats with delegation on
(`ds delegate on`):
- PostToolUse (Bash): a ds result block is recorded for this user prompt: OK with a passed check
  ("ok"), or not finished ("notdone": FAIL / PARTIAL / CONFLICT / BLOCKED).
- PreToolUse (any tool), same prompt only:
  * after "ok": running tests again is denied (the check already passed in the worker's copy);
  * after "notdone": only reads, ds commands, asking the user (AskUserQuestion) and loading tools
    pass; anything else is denied until the user has been asked, then the state clears.
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
BATCH_OK = re.compile(r"^BATCH\s+\S+.*\bnot_ok=0\b.*\bintegration=(none|pass)", re.M)


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


def new_prompt(p):
    sid = p.get("session_id")
    if not on(sid):
        return
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
    if NOT_DONE.search(out):
        save(sid, {"prompt": prompt, "state": "notdone"})
    elif OK_PASSED.search(out) or BATCH_OK.search(out):
        save(sid, {"prompt": prompt, "state": "ok"})


def pre(p):
    sid, prompt = p.get("session_id"), prompt_of(p)
    if p.get("agent_id") or not on(sid) or not prompt:
        return
    st = load(sid)
    if st.get("prompt") != prompt:
        return
    tool = p.get("tool_name") or ""
    cmd = (p.get("tool_input") or {}).get("command") or "" if tool == "Bash" else ""
    if st.get("state") == "ok":
        if tool == "Bash" and TEST_CMD.search(cmd) and not DS_ANY.search(cmd) and not DS_CMD.search(cmd):
            deny("ds already ran this check and it passed in the worker's copy: don't re-run tests. "
                 "Report the result, or start the next step.")
        return
    if st.get("state") == "notdone":
        if tool == "AskUserQuestion":
            save(sid, {})   # the user is being asked: whatever they pick is theirs
            return
        if tool in READ_TOOLS:
            return
        if tool == "Bash" and (DS_ANY.search(cmd) or DS_CMD.search(cmd) or READ_BASH.match(cmd)):
            return
        deny("The worker did not finish this step: ask the user first with one question (a Sonnet "
             "subagent finishes it from the found: note, ds revise with the findings, or stop), "
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
