# T4: Batches as the workflow tree

Work in the worktree `~/Work/ds-bridge-board` (branch `board`), never in `~/Work/ds-bridge` (that is `main`, and `~/.local/bin/ds` points into it, so every Claude chat runs that copy). If the worktree doesn't exist, create it: `git -C ~/Work/ds-bridge worktree add -b board ~/Work/ds-bridge-board main`, then copy `docs/board/` from `~/Work/ds-bridge` into it if it isn't committed there yet. Run `git status` first; changes under `docs/board/` are expected, stop and ask about anything else. Run the branch's ds as `~/Work/ds-bridge-board/ds` (plain `ds` is the live main copy). Don't run `./install.sh` from the worktree: it would re-point `~/.local/bin/ds` at the branch. Build the board with `cd board && go build -o ~/.local/bin/ds-board .`. Project notes are in `~/Work/ds-bridge/.claude/notes/` (not in the worktree).

Read `docs/board/SPEC.md` sections 3 and 4 and `docs/board/PROGRESS.md`.

cc-fleet shows a Dynamic Workflow run as run → phases → agents (`demo-workflow.webp`, `workflow-board.webp`, `workflow-leaf.webp` in `~/Work/cc-fleet/docs/assets/`; look at the frames). A ds batch maps onto it: batch = run, group = phase, the group's job = agent.

## Read first

- `dslib/batch.py` and the `batch.json` writer from T1.
- `board/internal/tui` run drill: `groupByRun` (view.go), `viewWfPhases`, `viewWfAgent`, `renderRunsBox`, `updateWfPhases`, `updateWfAgent`.

## Do

1. `dsjobs.ListRuns`: read `jobs/b*/batch.json`, return runs whose phases are the groups (label as the phase title, steps listed), with children jobs (job.json `batch` = run id) as the agents.
2. Phase status from its job, plus SKIPPED shown as cc-fleet's `stopped` look with the skip reason. Run header: title, short id, project, `N/M agents`, tokens, elapsed, as in cc-fleet.
3. Show the integration check result as the run's last line (cc-fleet has no slot; use the run header's right side or a final pseudo-phase "check". Pick the one closer to cc-fleet's look and say which in PROGRESS.md).
4. The Dynamic Workflows box title stays cc-fleet's unless the user asks to rename it to "Batches" (ask once).
5. Tests: fixture batch with 3 groups (one needs another, one SKIPPED).

## Don't

- Add restart (`r` on phase/agent). Dropped in SPEC 4.

## Done when

- `go test ./...` passes.
- Live: run a 2-step batch on a scratch project (`~/Work/ds-bridge-board/ds batch -C … plan.md`), watch it in `~/Work/ds-bridge-board/ds board`, capture it, show the user.
- PROGRESS.md T4 entry. Commit on `board` only when the user says so. Never push or merge to `main` without asking.
