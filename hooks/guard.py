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
"warn" mode and guard-off: only ~/.claude and /tmp/claude-* stay allowed, plus
small edits: at most "guard_lines" changed lines (default 20) per edit and
"guard_edits_per_file" edits (default 2) per file per turn. Delegating a
one-liner costs Claude more (brief + review) than making it. The per-turn
counts live in ~/.local/state/ds-bridge/edits/<session>.json, which the
delegation hook clears on every user prompt. Never breaks the session on
malformed input or other tools.
"""
import difflib
import fnmatch
import json
import os
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CONF = os.path.expanduser("~/.config/ds-bridge/config.json")
OFF = os.path.expanduser("~/.config/ds-bridge/guard-off")
DELEGATE = os.path.expanduser("~/.local/state/ds-bridge/delegate")
EDITS = os.path.expanduser("~/.local/state/ds-bridge/edits")
CLAUDE = os.path.realpath(os.path.expanduser("~/.claude"))
MESSAGE = (
    "ds-bridge guard: Claude doesn't make big edits to project files directly (up to "
    "20 changed lines and 2 edits per file per turn are fine). Delegate this "
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
    # Plans are how Claude delegates, so any size is fine (like cheap-ag's isPlan;
    # discipline.py and the playbook use the same .plan/ rule).
    if os.sep + ".plan" + os.sep in p:
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


def _int(key, default):
    try:
        return max(0, int(load_config().get(key, default)))
    except (TypeError, ValueError):
        return default


def _changed(old, new):
    """Changed lines between two texts: the larger of lines removed and lines added."""
    add = rem = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            add += 1
        elif line.startswith("- "):
            rem += 1
    return max(add, rem)


def _read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def edit_size(tool, ti, path):
    """Changed lines this edit would make, or None when it cannot be measured."""
    if tool == "Edit":
        old, new = ti.get("old_string"), ti.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        n = _changed(old, new)
        if ti.get("replace_all") and old:
            n *= max(1, (_read(path) or "").count(old))
        return n
    if tool == "MultiEdit":
        edits = ti.get("edits")
        if not isinstance(edits, list):
            return None
        total = 0
        for e in edits:
            sub = edit_size("Edit", e if isinstance(e, dict) else {}, path)
            if sub is None:
                return None
            total += sub
        return total
    if tool == "Write":
        new = ti.get("content")
        return _changed(_read(path) or "", new) if isinstance(new, str) else None
    if tool == "NotebookEdit":
        src = ti.get("new_source")
        return len(src.splitlines()) if isinstance(src, str) else None
    return None


def small_edit_ok(tool, ti, path, session_id):
    """True (and counted) when a delegating chat may make this edit itself."""
    limit = _int("guard_lines", 20)
    per_file = _int("guard_edits_per_file", 2)
    if not limit or not per_file or not isinstance(session_id, str) or not session_id.strip():
        return False
    n = edit_size(tool, ti, path)
    if n is None or n > limit:
        return False
    state_file = os.path.join(EDITS, session_id.strip() + ".json")
    try:
        with open(state_file) as f:
            counts = json.load(f)
        if not isinstance(counts, dict):
            counts = {}
    except (OSError, ValueError):
        counts = {}
    key = os.path.realpath(path)
    if int(counts.get(key, 0)) >= per_file:
        return False
    counts[key] = int(counts.get(key, 0)) + 1
    os.makedirs(EDITS, exist_ok=True)
    tmp = state_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(counts, f)
    os.replace(tmp, state_file)
    return True


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
        full = os.path.expanduser(path)
        if not os.path.isabs(full):
            full = os.path.join(payload.get("cwd") or os.getcwd(), full)
        if small_edit_ok(payload.get("tool_name"), tool_input, full, payload.get("session_id")):
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
