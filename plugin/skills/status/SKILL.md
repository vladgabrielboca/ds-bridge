---
name: status
description: Show whether delegation is on here, the worker model, fallback and takeover.
disable-model-invocation: true
allowed-tools: Bash(ds delegate *)
---

!`ds delegate status`

Show the user the status block above verbatim in a code block, nothing else.

If no output appears above, run that command yourself once with Bash. If ds errors, tell the user and stop.
