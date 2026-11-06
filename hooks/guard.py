#!/usr/bin/env python3
"""Claude Code PreToolUse guard: keep direct file edits out of the orchestrator.

Claude should delegate changes to the worker through ds, then review the diff.
This hook blocks Edit/Write/MultiEdit/NotebookEdit on project files and allows
Claude's own memory/settings, scratchpads and paths listed in
~/.config/ds-bridge/config.json under "guard_allow".

Reads the hook JSON from stdin. Exits 2 with a message to block; otherwise 0.
With "guard_mode": "warn" in the config, a would-be block becomes an allow with
a system message and extra context instead. A per-chat delegation marker
(`ds delegate on`, matched by the payload's session_id) overrides guard_allow,
"warn" mode and guard-off: only ~/.claude and /tmp/claude-* stay allowed. Never
breaks the session on malformed input or other tools.
"""
import fnmatch
import json
import os
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CONF = os.path.expanduser("~/.config/ds-bridge/config.json")
OFF = os.path.expanduser("~/.config/ds-bridge/guard-off")
DELEGATE = os.path.expanduser("~/.local/state/ds-bridge/delegate")
CLAUDE = os.path.realpath(os.path.expanduser("~/.claude"))
MESSAGE = (
    "ds-bridge guard: Claude doesn't edit project files directly. Delegate this "
    "change with `ds run -C <project> \"<brief>\"` (or `ds revise` for an open "
    "job), then review the diff."
)
WARN_CONTEXT = (
    "ds-bridge guard allowed this edit because guard_mode is \"warn\", but project "
    "changes are normally delegated with `ds run -C <project> \"<brief>\"`. Make "
    "sure this is a case your rules allow."
)

_CONFIG = None


def load_config():
    global _CONFIG
    if _CONFIG is None:
        try:
            with open(CONF) as f:
                data = json.load(f)
            _CONFIG = data if isinstance(data, dict) else {}
        except (OSError, ValueError, AttributeError):
            _CONFIG = {}
    return _CONFIG


def allow_globs():
    patterns = load_config().get("guard_allow") or []
    return [p for p in patterns if isinstance(p, str)]


def guard_mode():
    mode = load_config().get("guard_mode")
    return mode if mode in ("block", "warn") else "block"


def delegation_on(session_id):
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    return os.path.exists(os.path.join(DELEGATE, session_id.strip()))


def allowed(path, cwd, session_id=None):
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    p = os.path.realpath(p)

    if p == CLAUDE or p.startswith(CLAUDE + os.sep):
        return True
    if p.startswith("/tmp/claude-"):
        return True

    if delegation_on(session_id):
        return False

    if os.environ.get("DS_GUARD_OFF") == "1":
        return True
    if os.path.exists(OFF):
        return True

    for pattern in allow_globs():
        pattern = os.path.expanduser(pattern)
        if fnmatch.fnmatch(p, pattern):
            return True
    return False


def main():
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        if payload.get("tool_name") not in EDIT_TOOLS:
            return 0
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str) or not path:
            return 0
        if allowed(path, payload.get("cwd"), payload.get("session_id")):
            return 0
        if guard_mode() == "warn" and not delegation_on(payload.get("session_id")):
            line = (
                "ds-bridge guard: direct edit of %s (not delegated to ds)" % path
            )
            sys.stdout.write(json.dumps({
                "systemMessage": line,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": WARN_CONTEXT,
                },
            }) + "\n")
            return 0
        sys.stderr.write(MESSAGE + "\n")
        return 2
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
