# T5: Stop, delete, clear, pin

Work in the worktree `~/Work/ds-bridge-board` (branch `board`), never in `~/Work/ds-bridge` (that is `main`, and `~/.local/bin/ds` points into it, so every Claude chat runs that copy). If the worktree doesn't exist, create it: `git -C ~/Work/ds-bridge worktree add -b board ~/Work/ds-bridge-board main`, then copy `docs/board/` from `~/Work/ds-bridge` into it if it isn't committed there yet. Run `git status` first; changes under `docs/board/` are expected, stop and ask about anything else. Run the branch's ds as `~/Work/ds-bridge-board/ds` (plain `ds` is the live main copy). Don't run `./install.sh` from the worktree: it would re-point `~/.local/bin/ds` at the branch. Build the board with `cd board && go build -o ~/.local/bin/ds-board .`. Project notes are in `~/Work/ds-bridge/.claude/notes/` (not in the worktree).

Read `docs/board/SPEC.md` section 8 and `docs/board/PROGRESS.md`.

## Read first

- `board/internal/tui/confirm.go`, `pin.go`, and the `x`/`d`/`c`/`p` cases in model.go key handlers.
- `~/Work/cc-fleet/internal/subagent/job.go` `processAlive`, `cmdlineIsClaudeJob` (the pid-reuse guard to copy).
- `dslib/runner.py` `stop_worker`, `interrupt_session`, `mark_interrupted`; `dslib/core.py` SIGTERM handler (~line 180); `dslib/jobs.py` where job dirs are removed; `dslib/results.py` `gc_landed`, approve/reject cleanup (shadow refs, worktrees).

## Do

1. Python: hidden command `ds forget JOB...` that removes a closed job completely (job dir, shadow refs, worktree, progress file, pin), refuses open or running jobs with one line saying why, and works for batch ids (all children must be closed). Reuse the existing cleanup code paths; don't write a second one. Tests in `tests/test_ds.py`.
2. `x` stop on a running job or batch: confirm modal, check `/proc/<pid>/cmdline` is ds, SIGTERM. The card should show `failed · interrupted` on the next refresh (via `mark_interrupted`). Verify the OpenCode session really stops (the history notes say killing `opencode run` alone does not).
3. `d` delete: confirm modal, run `ds forget ID`, show its one line in the result modal.
4. `c` clear: closed + unpinned jobs of the focused session, count in the modal, one `ds forget` call with all ids.
5. `p` pin/unpin: `STATE/pins/<id>`, ★ on the row like cc-fleet.
6. Footer hints per level, matching the keys that exist now.

## Don't

- Delete anything from Go directly.
- Offer approve / reject / revise from the board.

## Done when

- Tests pass (`python3 tests/test_ds.py`, `go test ./...`).
- Live with the user: stop a running job from the board and confirm the OpenCode session stopped (`opencode session list` or the service API); delete a landed job; pin a landed job and run ds's gc with its age forced past the cutoff to show it survives.
- PROGRESS.md T5 entry. Commit on `board` only when the user says so. Never push or merge to `main` without asking.
