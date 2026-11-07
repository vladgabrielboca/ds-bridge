# ds-bridge planner playbook

The worker is literal, has no context (no skills, memories, CLAUDE.md) and fills every gap with a
guess. Leave no gaps.

## Before the brief
- Read the code; name exact places (file, function, quoted anchor line).
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
One action per step. No pronouns without a noun, no "similar to X", no vague words (lint refuses
them). Tell the worker to report BLOCKED with the step number instead of improvising.

## Review
Check the diff step by step against STEPS and DO NOT; trust the check section, not the worker's
CHECKS line. Then approve, revise (numbered points: step, file/place, exact correction) or
reject and rewrite smaller. Two failed revises: reject and split, or tell the user. Never fix by
hand while delegation is on.
