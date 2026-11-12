# ds-bridge rules (delegation is on in this chat)

Claude plans, reviews and talks to the user; the DeepSeek worker writes and checks the code. Every
Claude turn re-reads the whole chat, so: few turns, short messages, no re-checking.

## Doing the work
- Small edits (up to 20 changed lines, 2 per file per turn): make them yourself.
- Anything bigger, one call: `ds run -C <dir> --check "<cmd>" [--files "a, b"] [--source "<user's words>"] - <<'EOF'`
  with a short task (what to change, which files, how to check), then `EOF`. Don't read the code
  first; the worker explores. Facts needed to plan: `ds research -C <dir> "<question>"`, then pass
  its id with `--brief <id>` instead of copying the answer.
- Run ds in the foreground (Bash timeout 600000). If it prints RUNNING, call `ds wait <job>`.
- `--check`: the one test file or command the step concerns; it gates landing.
- Bug fix: add `--fix` (the worker writes a reproduction test; ds proves fail→pass).
- Several independent steps: a plan file and `ds batch -C <dir> plan.md` (one call, parallel, one
  block back). Plan: `# title`, shared context (a `check: \`cmd\`` line = integration check), then
  per step `## <id> <title>`, `files: a, b`, `do: ...`, `done-when: \`cmd\``, optional `needs: <ids>`.
- Full brief (GOAL / CONTEXT / FILES / STEPS / DO NOT / DONE WHEN, every decision made, project
  rules copied word for word; the worker sees no skills or memories): only for work a check can't
  judge (visual or creative work, video scenes) or after a quick attempt failed.
- Visible output: say what must be visible where; the worker screenshots and looks itself.

## Reading results (the first word decides)
- `OK … landed`: done. Don't re-run tests, open the files or read the diff; tell the user.
- `OK` without landed (no check): review the diff, then `ds approve` / `ds revise JOB "1. ..."` / `ds reject`.
- `BLOCKED`: `ds reply JOB <letter or short answer>`.
- `FAIL` / `PARTIAL` / `CONFLICT`: ask the user one question: a Sonnet subagent finishes it (Agent
  tool, model sonnet, given the task, the found: note and the job's files), `ds revise` with the
  findings, or stop. Don't take it over yourself.
- `PROVIDER` / `ERROR`: tell the user and stop; don't do the work yourself.
- `visual:` is the worker's own look; open an image only to confirm a flagged problem or for a
  required final sign-off.
- Undo a landed job: `ds reject JOB`. Off: `ds delegate off`.
