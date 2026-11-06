# Delegation to DeepSeek (ds-bridge): opt-in per chat

**Default: Claude works directly** (edits, builds, tests) in every chat. Delegation to DeepSeek via `ds` (OpenCode agent `worker`) is on only when the user turns it on for that chat: `/delegate`, or the user says "delegate to deepseek" / "use ds" -> run `ds delegate on`. `/delegate off` / "stop delegating" -> `ds delegate off`. While it's on, a hook reminds every turn and the guard blocks direct edits; Claude keeps only planning, architecture, troubleshooting/debugging, orchestration and review, and these rules apply:
- Execution = writing/editing code or files, refactors, mechanical edits, writing tests, running builds/tests to complete a change. Delegate it even when it's a one-liner; don't fix things yourself after review (no edits through Bash heredocs/sed either).
- Brief: `ds run -C <project> "<task>"`: self-contained, naming the files/functions, the exact change, and the acceptance check to run. Don't paste file contents; the worker reads them itself. Long brief: pass `-` and pipe it on stdin.
- Review only what ds prints (report + capped diff). Don't re-read the changed files; for a truncated part use `ds diff JOB <path>`. Run a check yourself only if the report's CHECKS look doubtful.
- Prefer `--check "<test cmd>"` so the bridge runs the acceptance check itself and the worker auto-fixes failures before you see the result; trust the check section over the worker's CHECKS line.
- Independent subtasks can run in parallel: start several `ds run ... --paths <their files>` with run_in_background, then review each report as it arrives. Jobs on overlapping paths are refused.
- Start every `ds run` / `ds revise` as a tracked background task (the Bash tool's run_in_background), never with a shell `&`: an untracked job's completion never reaches Claude, so its result sits unread.
- First job in a project without AGENTS.md: run `ds init -C <project>` and review it like any other job.
- Then exactly one of: `ds approve JOB`, `ds revise JOB "<numbered points>"` (same session; output shows only that round's diff), or `ds reject JOB` (restores the pre-job snapshot).
- If ds errors (quota, region, auth, timeout), tell the user and stop; don't silently do the work yourself.
