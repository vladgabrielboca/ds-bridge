---
name: delegate
description: Turn on DeepSeek delegation mode for this chat (or off with "/delegate off"). Use whenever the user types /delegate or says "delegate to deepseek", "delegate this", "use deepseek", "use ds", "hand it to the worker", or asks to stop delegating ("stop delegating", "do it yourself", "delegate off").
---

# Delegation mode

Argument: `off` turns it off, `status` reports it, and anything else (or no argument) turns it on.

1. Run `ds delegate on` (or `off` / `status`). It marks this chat via `$CLAUDE_CODE_SESSION_ID`.
2. `ds delegate on` prints the ds-bridge rules. Follow them for the rest of the chat (`ds playbook`
   prints them again; they come back by themselves after a compaction).
3. Confirm in one line, e.g. "Delegation on: I plan and review, DeepSeek writes the code."

If ds errors (quota, auth, timeout), tell the user and stop.
