# ds-bridge planner playbook

The worker is literal, has no context (no skills, memories, CLAUDE.md) and fills every gap with a
guess. Leave no gaps.

## Before the brief
- Don't open source files to plan. Ask `ds research -C <dir> "<question>"` (read-only, ≤200
  words + refs with verbatim anchor lines) and plan from its answer; open a file only when it
  is UNSURE or low confidence. Name exact places (file, function, quoted anchor line).
- Decide everything: names, values, strings, formats, order. No "choose"/"something like".
- One job ≤ ~3 files / ~150 lines; split bigger work (parallel with `--paths`).
- Copy the project rules that apply word for word (videos: the scene spec blocks + kit rules).

## Writing the brief
```
GOAL: <one sentence, the visible outcome>
CONTEXT: <current state, why, files to read first, copied rules>
FILES: <each file to edit + what changes; others "read only">
STEPS:
1. In <file>, <function/anchor>: <exact change, exact strings in backticks; input -> output example if non-trivial>
DO NOT: <files/behaviour to leave alone>
DONE WHEN: <command + expected output; also pass it as --check>
```
Visible output (UI, plot, render, page): add a LOOK step: the command that makes the image, its
path, and what must be visible where (element, position, text). Attach reference images with
`-f` (also on `revise`). The worker opens images itself; you read its VISUAL line, not the images.
One action per step. No pronouns without a noun, no "similar to X", no vague words (lint refuses
them). Tell the worker to report BLOCKED with the step number instead of improvising.

## Review
A passed `--check` is trusted: ds shows only the diff stat, so make DONE WHEN a real test of the
step. Without a check (or on FAIL) check the diff step by step against STEPS and DO NOT; trust
the check section, not the worker's CHECKS line. VISUAL is a first pass that can invent fine detail: open an image yourself only to
confirm a flagged problem or for the final sign-off the project requires. Then approve, revise (numbered points: step, file/place, exact correction) or
reject and rewrite smaller. Not finished (FAIL/BLOCKED): ask the user: Sonnet subagent from `found:`, revise, or stop.
Two failed revises: reject and split, or tell the user. Never fix by
hand while delegation is on.
