#!/usr/bin/env python3
"""Claude Code UserPromptSubmit reminder for per-chat delegation mode.

`ds delegate on` creates an empty marker under
~/.local/state/ds-bridge/delegate/<session_id>. When the hook's session_id has
that marker, every turn gets a short reminder to delegate execution to ds.
Otherwise it prints nothing. Never breaks the session: any exception exits 0.
"""
import json
import os
import sys

DELEGATE = os.path.expanduser("~/.local/state/ds-bridge/delegate")
REMINDER = (
    "Delegation mode is ON for this chat: every execution step (code/file edits, "
    "refactors, tests, builds to finish a change) goes through `ds run -C <project> "
    "...` started with run_in_background, including video projects; Claude only "
    "plans, reviews ds output and approves/revises/rejects. Turn off with "
    "`ds delegate off`."
)


def main():
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return 0
        session_id = session_id.strip()
        if not os.path.exists(os.path.join(DELEGATE, session_id)):
            return 0
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": REMINDER,
            },
        }) + "\n")
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
