# T1: Python side writes the board's data

Work in the worktree `~/Work/ds-bridge-board` (branch `board`), never in `~/Work/ds-bridge` (that is `main`, and `~/.local/bin/ds` points into it, so every Claude chat runs that copy). If the worktree doesn't exist, create it: `git -C ~/Work/ds-bridge worktree add -b board ~/Work/ds-bridge-board main`, then copy `docs/board/` from `~/Work/ds-bridge` into it if it isn't committed there yet. Run `git status` first; changes under `docs/board/` are expected, stop and ask about anything else. Run the branch's ds as `~/Work/ds-bridge-board/ds` (plain `ds` is the live main copy). Don't run `./install.sh` from the worktree: it would re-point `~/.local/bin/ds` at the branch. Build the board with `cd board && go build -o ~/.local/bin/ds-board .`. Project notes are in `~/Work/ds-bridge/.claude/notes/` (not in the worktree).

Read `docs/board/SPEC.md` first, sections 5 and 6. This ticket changes only Python in `dslib/` and `tests/test_ds.py`. No Go.

## Read first

- `dslib/runner.py`: `Tally` (class, ~line 430), `run_opencode`, `write_progress`, `write_progress_file`, `cleanup_progress`.
- `dslib/jobs.py`: job record creation (~line 244), `cmd_revise`, detach (`note_jid`, `detach_and_wait`).
- `dslib/results.py`: `parse_report`, the status words, `emit`, `record_ledger`, `gc_landed`.
- `dslib/batch.py`: `cmd_batch`, `run_group`.
- `~/Work/cc-fleet/internal/subagent/activity.go` (record format, throttling, arg clamp) and `~/Work/cc-fleet/internal/redact/redact.go` (patterns).
- `~/Work/ds-bridge/.claude/notes/ds-bridge-history.md` for pitfalls (stdin DEVNULL, PWD, session ids).

## Do

1. `activity.jsonl` per job, format in SPEC 6. Write from `Tally.feed`:
   - one `tool` record per tool event (OpenCode `tool_use`/`tool`). Arg = the same detail `Tally` already computes for `last` (shell command, path, pattern), passed through the ported redact patterns, clamped to 200 chars. File-writing tools: path only.
   - one `usage` record per `step_finish`, with `in` = input + cache_read of that step (the context size), `out` = cumulative output + reasoning for the job, `cache` = cache_read of that step.
   - add `round` from the job. Append-only across rounds; a new `ds revise` round appends.
   - writes are best-effort: catch OSError, never raise into the run.
   - also feed it from the overload retry / fork path and the failover path, so a job that switches session or model keeps one file.
2. `report.txt`: the worker's final text of each round, overwritten per round, written where `parse_report` gets its input.
3. At the end of each round, add to job.json: `steps`, `tools`, `cost` (sum over rounds), `tokens` `{in, out, cache}` (sums), `result` (the status word), `ended` (epoch). Keep every existing field.
4. `jobs/b<id>/batch.json` from `cmd_batch`, format in SPEC 6. Rewrite it atomically (tmp + `os.replace`) when a group starts, ends, or is SKIPPED.
5. Pins: `STATE/pins/<id>` zero-byte files. Make `gc_landed` and every other place that deletes job dirs skip pinned ids. Grep for `rmtree` and `os.remove` on JOBS paths to find them all.
6. Tests in `tests/test_ds.py` (worker-free, synthetic event streams like the existing ones): activity records for a fake stream with 2 steps and 3 tools, redaction of a fake `sk-…` key, report.txt written, job.json fields, batch.json shape for a 2-group plan (reuse how existing batch tests stub the worker, or test the writer function directly), pinned job survives `gc_landed`.

## Don't

- Change what Claude sees (the compact block, `last.txt`). Its size is the point of ds.
- Change the progress file format; the status line reads it.
- Write file contents or tool outputs into activity.jsonl.

## Done when

- `python3 tests/test_ds.py` passes, new checks included.
- One real job on a scratch project (`~/Work/ds-bridge-board/ds run -C /tmp/… --check "true" "…"`, foreground) produces `activity.jsonl`, `report.txt` and the new job.json fields. Show the user the files.
- `docs/board/PROGRESS.md` has a T1 entry: what changed, file formats as built, anything that differs from SPEC 6 (and SPEC updated to match).
- Commit on `board` only when the user says so. Never push or merge to `main` without asking.
