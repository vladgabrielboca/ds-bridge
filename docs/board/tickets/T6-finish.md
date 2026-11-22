# T6: Browser, polish, docs, full live test

Work in the worktree `~/Work/ds-bridge-board` (branch `board`), never in `~/Work/ds-bridge` (that is `main`, and `~/.local/bin/ds` points into it, so every Claude chat runs that copy). If the worktree doesn't exist, create it: `git -C ~/Work/ds-bridge worktree add -b board ~/Work/ds-bridge-board main`, then copy `docs/board/` from `~/Work/ds-bridge` into it if it isn't committed there yet. Run `git status` first; changes under `docs/board/` are expected, stop and ask about anything else. Run the branch's ds as `~/Work/ds-bridge-board/ds` (plain `ds` is the live main copy). Don't run `./install.sh` from the worktree: it would re-point `~/.local/bin/ds` at the branch. Build the board with `cd board && go build -o ~/.local/bin/ds-board .`. Project notes are in `~/Work/ds-bridge/.claude/notes/` (not in the worktree).

Read `docs/board/SPEC.md` and all of `docs/board/PROGRESS.md`.

## Do

1. `ctrl+f` browser (cc-fleet browser.go): flat newest-first list of jobs and batches across all sessions, type to filter over title / model / project / status, `⏎` opens the card, `esc` returns.
2. Edge states: state dir missing (fresh install) → a centered "no ds jobs yet" frame, not an error; malformed job.json → skip the row and show cc-fleet's dim jobs-error line; terminal narrower than 80 columns → whatever cc-fleet does, nothing worse.
3. Colors: ask the user SPEC 10 question 1 and apply the answer.
4. Compare against cc-fleet's demo frames (`~/Work/cc-fleet/docs/assets/*.webp`, extracted with `magick X.webp -coalesce f_%03d.png`). There's no need to run `ccf` itself. List every visible difference in PROGRESS.md and fix the unintended ones.
5. Docs: README section "Board" (what it shows, `ds board`, keys, license note for the cc-fleet-derived code); `ds` help text line for `board`; history note entry in `~/Work/ds-bridge/.claude/notes/ds-bridge-history.md`.
6. Full live run with the user, all started with `~/Work/ds-bridge-board/ds`: 2 parallel `run` jobs + 1 `research` + 1 small `batch` in one Claude session, plus 1 job from another session. Board open in a tmux split the whole time. Capture frames at start, mid, end.

## Done when

- All tests pass.
- Merge, only after the user OKs it: `git -C ~/Work/ds-bridge merge board`, then `./install.sh` in `~/Work/ds-bridge` with `~/.local/bin/ds-board` deleted first, to prove the installer builds it. Then remove the worktree (`git worktree remove ~/Work/ds-bridge-board`) if the user agrees.
- The live run shows every job correctly grouped, live activity, final outputs, and the batch tree.
- PROGRESS.md marks the project done and lists open follow-ups (T7, key binding).
- Commit on `board` only when the user says so. Never push or merge to `main` without asking.
