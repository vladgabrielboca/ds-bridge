---
name: flash
description: Delegate this chat's coding to DeepSeek Flash (the default worker).
disable-model-invocation: true
allowed-tools: Bash(ds delegate *)
---

!`ds delegate on --model flash`

Show the user the status block above (the lines from "ds delegation" to "switch:") verbatim in a code block, nothing else. If rules are printed below the block, follow them for the rest of the chat.

If no output appears above, run that command yourself once with Bash. If ds errors, tell the user and stop.
