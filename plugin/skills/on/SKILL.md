---
name: on
description: Turn on DeepSeek delegation for this chat. Use for /ds:on or "delegate to deepseek", "use ds", "hand it to the worker".
allowed-tools: Bash(ds delegate *)
---

!`ds delegate on`

Delegation is on for this chat. Show the user the status block above (the lines from "ds delegation" to "switch:") verbatim in a code block, nothing else. Follow the rules printed below it for the rest of the chat (`ds playbook` prints them again).

If no output appears above, run that command yourself once with Bash. If ds errors, tell the user and stop.
