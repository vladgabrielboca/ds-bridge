---
name: delegate
description: Turn on DeepSeek delegation mode for this chat (or off with "/delegate off"). Use whenever the user types /delegate or says "delegate to deepseek", "delegate this", "use deepseek", "use ds", "hand it to the worker", or asks to stop delegating ("stop delegating", "do it yourself", "delegate off").
---

# Delegation mode

Argument: `off` turns it off, `status` reports it, and anything else (or no argument) turns it on.

1. Run `ds delegate on` (or `off` / `status`). It marks this chat via `$CLAUDE_CODE_SESSION_ID`.
2. Confirm in one line, e.g. "Delegation mode on: I'll plan and review, DeepSeek does the edits."

While the mode is on (a hook repeats a reminder every turn, and the guard blocks direct edits outside `~/.claude` and the scratchpad):

- Every execution step goes to `ds run -C <project> "<brief>"`, always started with Bash
  `run_in_background`, never a shell `&`. This includes video projects: the user turned it on in
  this chat, which overrides the video exception.
- Briefs must be self-contained: the files and functions involved, the exact change, and an
  acceptance check, preferably as `--check "<cmd>"`. Pass a long brief on stdin with `-`.
- Run independent subtasks in parallel with `--paths`, keeping their files from overlapping.
- Review only the ds output (the report plus the diff, using `ds diff JOB <path>` for any
  truncated part). Then do exactly one of `ds approve JOB`, `ds revise JOB "<numbered points>"`
  or `ds reject JOB`. Don't fix the result by hand.
- If ds errors (quota, auth, timeout), tell the user and stop.
- First job in a project without an AGENTS.md: run `ds init -C <project>` first.

Claude itself only plans, designs, debugs, orchestrates and reviews.
