# T2: Go scaffold, board code copied and stripped

Work in the worktree `~/Work/ds-bridge-board` (branch `board`), never in `~/Work/ds-bridge` (that is `main`, and `~/.local/bin/ds` points into it, so every Claude chat runs that copy). If the worktree doesn't exist, create it: `git -C ~/Work/ds-bridge worktree add -b board ~/Work/ds-bridge-board main`, then copy `docs/board/` from `~/Work/ds-bridge` into it if it isn't committed there yet. Run `git status` first; changes under `docs/board/` are expected, stop and ask about anything else. Run the branch's ds as `~/Work/ds-bridge-board/ds` (plain `ds` is the live main copy). Don't run `./install.sh` from the worktree: it would re-point `~/.local/bin/ds` at the branch. Build the board with `cd board && go build -o ~/.local/bin/ds-board .`. Project notes are in `~/Work/ds-bridge/.claude/notes/` (not in the worktree).

Read `docs/board/SPEC.md` first, sections 3, 4, 7 and 9. This ticket creates `board/` and gets cc-fleet's board compiling with a stub data layer. No Python changes except `ds board`.

## Read first

- `~/Work/cc-fleet/internal/tui/` all non-test files. Start with the `Model` struct (model.go:106), `Update` (model.go:1568), `View` (view.go:75), `renderAsFooter` (view.go:618).
- `~/Work/cc-fleet/go.mod` for exact dependency versions.
- `~/Work/cc-fleet/LICENSE`.
- Look at frames of `~/Work/cc-fleet/docs/assets/demo-tui.webp` and `demo-subagent.webp` (`magick X.webp -coalesce f_%03d.png`, read a few) so you know what must survive the stripping.

## Do

1. Create `board/` per SPEC 7: `go.mod` (module `ds-bridge/board`, same charmbracelet versions as cc-fleet), `main.go`, `LICENSE-cc-fleet`, `NOTICE`.
2. Copy these from cc-fleet into `board/internal/tui/`: model.go, view.go, confirm.go, browser.go, pin.go, screen_handlers.go, workflow_events.go, and their `_test.go` files that test board behavior. Add the "Derived from cc-fleet … (Apache-2.0); modified." header to each.
3. Copy `internal/sessiontitle/` (inline its one `homedir` import) and `internal/pinned/` (replace its `config`/`fileutil`/`ids` imports with small local helpers; pins dir comes from `--state`, `STATE/pins/`).
4. Delete everything SPEC 9 lists as out of scope: provider hub and forms, model picker, key manager, Codex auth, setup and install nudges, Agent Teams (teammates, panes, inboxes, `teammate_events.go`, `panevis`, `teardown`, `teamhist`), save-workflow, restart. The board opens straight on the Projects level; `tab`/`esc` at the top quit instead of going to providers.
5. Create `board/internal/dsjobs/` with the `Result` and `WorkflowRun` types holding exactly the fields the remaining tui code reads (SPEC 7 lists them), and stub `ListJobs`, `ListRuns`, `ReadIO`, `ReadActivity` that return fixed sample data. Point every former `subagent.`/`workflow.` reference at `dsjobs`.
6. `ds board` in `dslib/cli.py`: exec `~/.local/bin/ds-board` with the remaining args; if missing, print `ds-board not built: run ./install.sh (needs go)` and exit 1.
7. `install.sh`: if `go` is on PATH, `go build -o ~/.local/bin/ds-board ./board` (from the repo dir); else print one skip line. Keep the installer idempotent.

## Don't

- Rename cc-fleet types or functions that stay. Upstream diffs must stay readable.
- Redesign layout, colors or keys.
- Touch job data on disk; the stub returns fixed data.

## Done when

- `cd board && go vet ./... && go test ./...` pass (drop tests only for deleted features; note each dropped test file in PROGRESS.md).
- `bash -n install.sh` passes and the new build lines are shown to the user (the installer itself runs only after the merge to `main`). `cd board && go build -o ~/.local/bin/ds-board .` works, and `~/Work/ds-bridge-board/ds board` shows the stub data: projects → session → jobs box → job card with Prompt, Activity, Output, Outcome. Take a screenshot (`grim` or the user's screenshot tool) or run it in tmux and `tmux capture-pane -p`, and show the user.
- `docs/board/PROGRESS.md` has a T2 entry: files copied, files deleted, line counts before/after, anything left that T3 must replace.
- Commit on `board` only when the user says so. Never push or merge to `main` without asking.
