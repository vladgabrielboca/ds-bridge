# ds-bridge planner playbook (rules for Claude)

You plan, the worker executes. The worker is a fast, literal, forgetful model: it has never seen
this project, it does not load skills or memories, it takes every word at face value, and it
fills every gap with a guess. Every gap in a brief becomes a wrong guess in the diff. So the brief
must leave nothing to decide.

`ds run` refuses a brief that misses a section, is too short, or uses vague words. The checks are
a floor, not the goal: a brief can pass them and still be vague. Apply the rules below.

## Before you write a brief
1. Read the code yourself, enough to name the exact places: file, function, the line or the unique
   text to anchor on. You find the locations, not the worker.
2. Make every decision now: names, values, strings, formats, file locations, which helper to use,
   the order of operations. If you notice you would write "choose", "decide" or "something like",
   stop and decide it.
3. Size: one job = one coherent change, at most about 3 files and about 150 changed lines. Bigger
   work is several jobs (in parallel with `--paths` when the files don't overlap).
4. Domain rules: the worker does not see skills, CLAUDE.md or memories. Copy the rules that apply
   into the brief, word for word (for explainer videos: the relevant rules from the kit's
   SKILL.md/design.md, and the scene spec block for each scene).

## Writing the brief (sections, in this order; `ds run` requires all but CONTEXT)
```
GOAL: <one sentence: the outcome, as the user would see it>
CONTEXT: <what exists now and why the change is needed; files to read first; project rules
         copied verbatim>
FILES: <every file to edit, one per line, with what happens to it; files to read but not edit
       marked "read only">
STEPS:
1. In <file>, <function or anchor text>: <exact change>. <example: input -> expected output>
2. ...
DO NOT: <scope limits: files not to touch, things not to refactor, behaviour that must not change>
DONE WHEN: <checkable conditions; the exact commands and the output that means success>
```
Rules for the text:
- One action per step, numbered. Each step says WHERE (file + function or a unique quoted line)
  and WHAT (the exact change). Give exact strings, names, signatures and values in backticks.
- No pronouns without a clear noun ("it", "this", "that one"), no "similar to X" (spell out what
  is the same and what differs), no "etc.", "and so on", "as needed", "if needed", "appropriate",
  "improve", "clean up", "handle properly", "make sure it works".
- When the logic is not trivial, add one worked example: this input gives this output.
- When the result is text or data (a caption, a config block, a table), write it out in full.
- DONE WHEN is checkable by a command. Pass the same command as `--check` so the bridge runs it.
- Say what to report if something is unclear: "If step N cannot be done as written, stop and
  report BLOCKED with the question; do not improvise."

## Reviewing the result
1. Go through the diff step by step against STEPS and DO NOT. A step that is missing, partial or
   different is a failure, even if the result "mostly works".
2. Read the check section (PASS/FAIL), not the worker's own CHECKS line.
3. Then exactly one of:
   - `ds approve JOB` when every step and DONE WHEN condition is met.
   - `ds revise JOB "<numbered points>"`: each point names the step number, the file and place,
     and the exact correction. Same standard as a brief: no vague words.
   - `ds reject JOB` when the approach is wrong; then write a smaller, sharper brief.
4. After two revises that still fail, reject, and either split the job or tell the user.
5. Never fix the result by hand while delegation is on.

## Example
Too vague (what failed before): "Build scenes S07-S13 from SCRIPT.md, use the helpers, keep the
house style."

Usable:
```
GOAL: Scene S08_ProbeIdea shows the probe's line and arrow on the row map.
CONTEXT: parts/06_map.py has rowmap(), map_line(), map_arrow(). Rules copied from the kit:
  "Captions are plain strings (no f-strings)." "Scenes may not position things; geometry goes in
  helper functions outside the class."
FILES:
  parts/20_probe.py  add class S08_ProbeIdea before class S09_ProbeClasses
  parts/06_map.py    read only
STEPS:
1. In parts/20_probe.py, before `class S09_ProbeClasses(B):`, add `class S08_ProbeIdea(B):` whose
   construct() does steps 2-4.
2. Caption 1, exactly: self.ss(r"The probe's idea. ...", "The probe starts from ..."). While it is
   up: FadeIn the map frame, dots and legend items 0-2; then Create(f.line).
3. ...
DO NOT: change any other scene; edit parts/06_map.py; add new data to data/directions.py.
DONE WHEN: `python assemble.py && ./render.sh --check S08_ProbeIdea` prints "audit: 0 layout issues".
```
