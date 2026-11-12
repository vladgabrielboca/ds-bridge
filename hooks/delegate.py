#!/usr/bin/env python3
"""Claude Code UserPromptSubmit reminder for per-chat delegation mode.

`ds delegate on` creates a marker under ~/.local/state/ds-bridge/delegate/<session_id>
and prints the planner playbook. When the hook's session_id has that marker:
- UserPromptSubmit: a one-line reminder every turn; the whole playbook only if the
  marker is still empty (the playbook was never shown in this chat).
- SessionStart with source "compact": the playbook again, since compaction may
  have summarised it away.
Otherwise it prints nothing. Never breaks the session: any exception exits 0.
"""
import json
import os
import sys

DELEGATE = os.path.expanduser("~/.local/state/ds-bridge/delegate")
EDITS = os.path.expanduser("~/.local/state/ds-bridge/edits")
REMINDER = (
    "Delegation ON: edits over 20 lines go through `ds run -C <dir> -` (run_in_background); "
    "learn code with `ds research`, not Read; briefs follow the planner playbook (reread: "
    "`ds playbook`); then approve/revise/reject. Off: `ds delegate off`."
)
PLAYBOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                        "planner-playbook.md")


def playbook():
    """The playbook text, or a visible warning: a missing file must not fail silently."""
    try:
        with open(PLAYBOOK) as f:
            return f.read()
    except OSError as e:
        return (f"WARNING: ds-bridge planner playbook not readable at {PLAYBOOK} ({e}). "
                "Run `ds playbook`, or tell the user the playbook is missing.")


def out(event, text):
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": text},
    }) + "\n")


# Marker state (JSON in the marker file; older markers hold "" or plain text):
#   shown    the playbook was shown in this chat
#   offset   bytes of the transcript already scanned for compaction records
#   covered  compaction ids whose aftermath already got the playbook
#   pending  a SessionStart(compact) injected it; the next new compaction record is covered
def load_state(marker):
    try:
        raw = open(marker).read().strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        st = json.loads(raw)
        return st if isinstance(st, dict) else {"shown": True}
    except ValueError:
        return {"shown": True}


def save_state(marker, st):
    st["covered"] = st.get("covered", [])[-20:]
    tmp = marker + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, marker)


def new_compactions(st, transcript):
    """Ids of compaction records added to the transcript since the last scan. The first
    scan of a chat only sets the offset: compactions before tracking started are covered by
    the playbook shown when delegation was turned on."""
    if not isinstance(transcript, str) or not os.path.isfile(transcript):
        return []
    size = os.path.getsize(transcript)
    start = st.get("offset")
    if start is None or start > size:          # first scan, or the file was replaced
        st["offset"] = size
        return []
    ids = []
    with open(transcript, "rb") as f:
        f.seek(start)
        data = f.read(size - start)
    end = data.rfind(b"\n") + 1                 # leave a half-written last line for later
    for line in data[:end].splitlines():
        if b'"compact_boundary"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("subtype") == "compact_boundary":
            ids.append(d.get("uuid") or f"at{start}")
    st["offset"] = start + end
    return ids


def main():
    marker = None
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return 0
        marker = os.path.join(DELEGATE, session_id.strip())
        if not os.path.exists(marker):
            return 0
        st = load_state(marker)
        event = payload.get("hook_event_name") or "UserPromptSubmit"
        if event == "SessionStart":
            # compaction summarised the old context: the playbook may be gone, bring it back
            if payload.get("source") == "compact":
                out("SessionStart", "Delegation mode is ON. Planner playbook (after compaction):\n"
                    + playbook())
                st["shown"] = True
                st["pending"] = True
                save_state(marker, st)
            return 0
        # a new user turn: the guard's small-edit counts start over
        try:
            os.remove(os.path.join(EDITS, session_id.strip() + ".json"))
        except OSError:
            pass
        # UserPromptSubmit: one line, plus the playbook if this chat never saw it or a
        # compaction happened that the SessionStart hook did not cover (backup guard)
        text = REMINDER
        fresh = [i for i in new_compactions(st, payload.get("transcript_path"))
                 if i not in st.get("covered", [])]
        if fresh and st.pop("pending", False):
            st.setdefault("covered", []).append(fresh.pop(0))
        if not st.get("shown"):
            text += "\n\nPlanner playbook (shown once per chat):\n" + playbook()
        elif fresh:
            text += "\n\nPlanner playbook (a compaction happened; shown again):\n" + playbook()
        st["shown"] = True
        st.setdefault("covered", []).extend(fresh)
        save_state(marker, st)
        out("UserPromptSubmit", text)
        return 0
    except Exception as e:
        # never break the session, but don't fail silently while delegation is on
        try:
            if marker and os.path.exists(marker):
                out(payload.get("hook_event_name") or "UserPromptSubmit",
                    REMINDER + f" (ds-bridge hook error: {e!r}; run `ds playbook` if the "
                    "playbook is not in context.)")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
