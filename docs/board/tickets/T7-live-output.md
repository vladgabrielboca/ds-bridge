# T7 (optional, only with the user's OK): worker text live in Output

Work in the worktree `~/Work/ds-bridge-board` (branch `board`), never in `~/Work/ds-bridge` (that is `main`, and `~/.local/bin/ds` points into it, so every Claude chat runs that copy). If the worktree doesn't exist, create it: `git -C ~/Work/ds-bridge worktree add -b board ~/Work/ds-bridge-board main`, then copy `docs/board/` from `~/Work/ds-bridge` into it if it isn't committed there yet. Run `git status` first; changes under `docs/board/` are expected, stop and ask about anything else. Run the branch's ds as `~/Work/ds-bridge-board/ds` (plain `ds` is the live main copy). Don't run `./install.sh` from the worktree: it would re-point `~/.local/bin/ds` at the branch. Build the board with `cd board && go build -o ~/.local/bin/ds-board .`. Project notes are in `~/Work/ds-bridge/.claude/notes/` (not in the worktree).

cc-fleet shows Output only after the job ends. This ticket streams the worker's text into the card while it runs.

## Do

1. Python: append the worker's text deltas (OpenCode `text` events, the ones `Tally.feed` collects into `texts`) to `jobs/<id>/live.txt` per round, best-effort, capped at 256 KB (keep the tail).
2. Board: while running, Output shows the tail of `live.txt` that fits the card, auto-scrolled to the bottom unless the user scrolled with `j/k`; when done, switch to `report.txt` as today.
3. Tests for the cap and the switch.

## Done when

- Live check: a job writing a long answer shows text growing in the card.
- PROGRESS.md T7 entry. Commit on `board` only when the user says so. Never push or merge to `main` without asking.
