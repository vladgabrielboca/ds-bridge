# T3: Real data for single jobs

Work in the worktree `~/Work/ds-bridge-board` (branch `board`), never in `~/Work/ds-bridge` (that is `main`, and `~/.local/bin/ds` points into it, so every Claude chat runs that copy). If the worktree doesn't exist, create it: `git -C ~/Work/ds-bridge worktree add -b board ~/Work/ds-bridge-board main`, then copy `docs/board/` from `~/Work/ds-bridge` into it if it isn't committed there yet. Run `git status` first; changes under `docs/board/` are expected, stop and ask about anything else. Run the branch's ds as `~/Work/ds-bridge-board/ds` (plain `ds` is the live main copy). Don't run `./install.sh` from the worktree: it would re-point `~/.local/bin/ds` at the branch. Build the board with `cd board && go build -o ~/.local/bin/ds-board .`. Project notes are in `~/Work/ds-bridge/.claude/notes/` (not in the worktree).

Read `docs/board/SPEC.md` first, sections 4, 5 and 6, and `docs/board/PROGRESS.md` (T1 and T2 entries: the file formats as actually built, and what the stub left open).

## Read first

- `board/internal/dsjobs/` (the stub from T2) and the tui calls into it.
- `~/Work/cc-fleet/internal/subagent/job.go` `ListJobs`, `StatusFor`, `readMeta`; `internal/tui/workflow_events.go` `readLeafActivity`, `readLeafIO`; `internal/tui/model.go` `loadBoard` (473), `groupSessions` (900), `groupProjects` (858).
- `dslib/info.py` `cmd_list` / `cmd_status` (how ds itself reads jobs and progress today).

## Do

1. Replace the stub with readers for SPEC 6 files under `--state`:
   - `ListJobs`: every `jobs/<id>/job.json` (skip `b*` dirs here), merged with `progress/<id>.json` and the first word of `last.txt`. Status per SPEC 5, including the dead-pid rule (check `/proc/<pid>`).
   - `LeadSessionID` = `owner`; project dir = `project` or else `dir`; `StartedAt` from `started`; elapsed from progress `updated`/job `ended`.
   - `ReadIO(id)`: prompt = job `task`, answer = `report.txt` (or `answer.txt` for research jobs).
   - `ReadActivity(id)`: `activity.jsonl` into the existing `activitySnapshot` (sigs `tool(arg)`, latest usage). Jobs from before T1 have no file: show `(no activity recorded)`, not an error.
   - Tokens: live from activity usage while running, job.json `tokens` when done. Cost from job.json `cost` when > 0. Turns = `steps`. Stop = `result`. Round shown as `round N` when > 1 (where cc-fleet shows attempt).
2. Session titles and project grouping via the ported `sessiontitle` (same as cc-fleet). Jobs with empty owner go in the "no session" group.
3. Keep cc-fleet's refresh: 3 s full, 500 ms while a job runs.
4. Unit tests with a fixture state dir in `board/internal/dsjobs/testdata/`: one running job (alive pid = the test's own pid), one dead-pid running job, one landed, one open OK, one BLOCKED, one research job, one pre-T1 job with no activity file.

## Don't

- Write anything to the state dir. This ticket is read-only.
- Show batch children as plain jobs if they carry `batch`; leave them out until T4 (note it in PROGRESS.md).

## Done when

- `go test ./...` passes.
- Live check with the user: start a real job in the background (`~/Work/ds-bridge-board/ds run -C <scratch project> --check "…" "…"`), open `~/Work/ds-bridge-board/ds board` in a tmux split, and show that the card's Activity and tokens move while it runs and Output/Outcome fill in when it ends. Capture before/after (`tmux capture-pane -p` or screenshots).
- PROGRESS.md T3 entry.
- Commit on `board` only when the user says so. Never push or merge to `main` without asking.
