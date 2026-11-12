---
name: muse
description: Delegate this chat's coding to Muse Spark (faster, slightly weaker than Flash).
disable-model-invocation: true
allowed-tools: Bash(ds delegate *)
---

!`ds delegate on --model muse`

Show the user the status block above (the lines from "ds delegation" to "switch:") verbatim in a code block, nothing else. Every ds job in this chat now runs on Muse unless a command passes -m. If rules are printed below the block, follow them for the rest of the chat.

If no output appears above, run that command yourself once with Bash. If ds errors, tell the user and stop.
