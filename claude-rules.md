# Delegation to DeepSeek (ds-bridge) — no exceptions

All execution goes to DeepSeek via `ds` (OpenCode agent `worker`). Claude keeps only planning, architecture, troubleshooting/debugging, orchestration and review.
- Execution = writing/editing code or files, refactors, mechanical edits, writing tests, running builds/tests to complete a change. Delegate it even when it's a one-liner; don't fix things yourself after review.
- Brief: `ds run -C <project> "<task>"`: self-contained, naming the files/functions, the exact change, and the acceptance check to run. Don't paste file contents; the worker reads them itself. Long brief: pass `-` and pipe it on stdin.
- Review only what ds prints (report + capped diff). Don't re-read the changed files; for a truncated part use `ds diff JOB <path>`. Run a check yourself only if the report's CHECKS look doubtful.
- Prefer `--check "<test cmd>"` so the bridge runs the acceptance check itself and the worker auto-fixes failures before you see the result; trust the check section over the worker's CHECKS line.
- Independent subtasks can run in parallel: start several `ds run ... --paths <their files>` with run_in_background, then review each report as it arrives. Jobs on overlapping paths are refused.
- First job in a project without AGENTS.md: run `ds init -C <project>` and review it like any other job.
- Then exactly one of: `ds approve JOB`, `ds revise JOB "<numbered points>"` (same session; output shows only that round's diff), or `ds reject JOB` (restores the pre-job snapshot).
- If ds errors (quota, region, auth, timeout), tell the user and stop; don't silently do the work yourself.
- A PreToolUse hook blocks direct Edit/Write on project files; that's intended, so delegate instead of working around it (no edits through Bash heredocs/sed either).
