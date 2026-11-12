---
name: off
description: Turn off DeepSeek delegation for this chat. Use for /ds:off or "stop delegating", "do it yourself".
allowed-tools: Bash(ds delegate *)
---

!`ds delegate off`

Delegation is off: from now on work directly (edit, build, test yourself) and ignore the ds-bridge rules shown earlier in this chat. Show the user the status block above verbatim in a code block, nothing else.

If no output appears above, run that command yourself once with Bash. If ds errors, tell the user and stop.
