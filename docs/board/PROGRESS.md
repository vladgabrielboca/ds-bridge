# ds board: progress

One entry per ticket, newest last. Spec: `docs/board/SPEC.md`. Tickets: `docs/board/tickets/`.

## T0 (2026-11-22): spec and tickets

- cc-fleet cloned to `~/Work/cc-fleet` at `29262a6` as the reference. Go 1.27.4 installed.
- Spec written from the cc-fleet code (tui, subagent, sessiontitle, pinned, redact) and the demo frames, plus the ds-bridge code (runner, jobs, batch, results, info).
- Open questions for the user: SPEC section 10.

## T1 (2026-11-22): Python writes the board's data

Branch `board` in the worktree `~/Work/ds-bridge-board` (created this session from `main` 9b2a494). Not committed yet.

What changed.

- `dslib/runner.py`: `Activity` writes `jobs/<id>/activity.jsonl`, fed from `Tally.feed`. `activity_for(job)` keeps one writer per job dict, so overload retries, forks, failover, fix rounds and the wrap-up all append to the same file. A new round (revise, new process) reads the file's last lines and continues `seq` and the `out` total. `mask_keys` is the port of cc-fleet `MaskKeyLikeString` (sk-, Bearer, x-api-key). Tools `edit`, `write`, `patch`, `apply_patch`, `multiedit` log the path only.
- `dslib/results.py`: `write_report` (report.txt, in `finish_job`), `end_round` (round fields in job.json), `is_pinned` + `PINS`, `update_batch` / `set_batch_job` (locked read-modify-write of batch.json). `gc_landed` skips pinned ids.
- `dslib/jobs.py`: research jobs also write report.txt and the round fields. The two `rmtree` calls on a fresh job's failed setup skip pinned ids (the only places ds deletes a job dir). A batch child records `batch_group` and names its job in batch.json.
- `dslib/batch.py`: batch.json written at start, on each group's start, end or SKIPPED, and at the end. Children get `DS_BATCH_GROUP`.
- `tests/test_ds.py`: 10 new checks (activity records, masking and clamp, path-only edits, revise append, report.txt, job.json sums, pinned gc, batch.json from a real `cmd_batch` with a fake ds).

Formats as built (example from job 1122-162535, a real DeepSeek run).

```
{"seq":1,"kind":"tool","tool":"shell","arg":"ls -la && cat calc.py 2>/dev/null","round":1,"ts":"2026-11-22T14:25:39Z"}
{"seq":2,"kind":"usage","in":10950,"out":49,"cache":0,"round":1,"ts":"2026-11-22T14:25:39Z"}
{"seq":3,"kind":"tool","tool":"edit","arg":"calc.py","round":1,"ts":"2026-11-22T14:25:41Z"}
```

job.json then had `"steps": 4, "tools": 3, "cost": 0.0, "tokens": {"in": 11495, "out": 235, "cache": 33152}, "result": "OK", "ended": 1795357550`.

Differences from the first SPEC 6 (SPEC updated to match).

- Tool `arg` is Tally's detail (shell command, else path / filePath / pattern), not cc-fleet's "first input value"; other tools get an empty arg. The OpenCode shell tool may be named `shell` or `bash`; both show the command.
- Usage `out` is cumulative over the whole job, across rounds.
- `steps` and `tools` are summed over rounds like `cost` and `tokens`.
- batch.json has extra `integration` and `fix_job`, group status starts at `queued`, `needs` holds only `needs:` deps.
- job.json of a batch child has `batch_group`.
- `cost` was 0.0 in the real run (OpenCode Go plan, step_finish carried no cost); the board should show 0 as is, not as missing data.
- `last` in the progress file is unchanged (only `shell` shows its command there), and `last.txt` is unchanged.

## T2 (2026-11-22): Go scaffold, board on stub data

Same branch and worktree, not committed yet.

What was made.

- `board/` module `ds-bridge/board` (go 1.27): `main.go` (`--state`, default `~/.local/share/ds-bridge`, and `--version`), `LICENSE-cc-fleet`, `NOTICE` (cc-fleet commit 29262a6).
- `board/internal/tui/`: model.go, view.go, confirm.go, browser.go, pin.go, screen_handlers.go, workflow_events.go and their tests, each with the "Derived from cc-fleet … (Apache-2.0); modified." header.
- `board/internal/sessiontitle/` (homedir inlined as `os.UserHomeDir`) and `board/internal/pinned/` (Kinds Job and Run only, one flat dir `STATE/pins/`, local `validID` and `atomicWrite`, Purge removed).
- `board/internal/dsjobs/`: `Result`, `WorkflowRun`, `RunPhase`, `Usage`, `Activity`, and stub `ListJobs`, `ListRuns`, `ReadIO`, `ReadActivity` returning fixed sample data (2 projects, 3 sessions, 6 jobs, 1 batch).
- `ds board` in `dslib/cli.py` execs `~/.local/bin/ds-board` with the rest of the args, checked before argparse. Missing binary prints `ds-board not built: run ./install.sh (needs go)` and exits 1.
- `install.sh`: `build_board` runs after `link_ds`. It builds with go when go is on PATH, else prints one skip line. A failed build warns and the install goes on.

Line counts of the 7 copied tui files: 9308 in cc-fleet, 5195 now (model.go 4683 to 2133, view.go 3438 to 2127, confirm.go 390 to 298, browser.go 548 to 468, pin.go 72, workflow_events.go 114 to 48, screen_handlers.go 63 to 49).

Deleted, per SPEC 9.

- Provider hub, model picker, keys and rotation, Codex auth, setup and Claude install nudges, the save-workflow name prompt, run, phase and agent restart.
- Agent Teams: teammate rows, the team box, h/s hide and show, team pins, ended-team history, the tmux-missing notice.
- cc-fleet files never copied: form.go, templates.go, installclaude.go, teammate_events.go, verbose*.go.
- Key changes from that: the board opens on Projects. tab, q, and esc at the top level quit. r, R and ctrl+r all refresh. The session view has 2 boxes (Dynamic Workflows, Subagents) instead of 3. The title reads `ds · Agents Board    tab/q quit`.

Tests. `go vet ./...` and `go test ./...` pass. tui tests went from 212 to 83 (one skipped). The full list of dropped tests with the compile error that dropped each is in `docs/board/t2-dropped-tests.txt`.

- model_test.go 127 to 20, workflow_board_test.go 50 to 34, browser_test.go 22 to 17, pin_board_test.go 9 (ClearFinished modal skipped until T5), session_label_test.go 1, path_split_test.go 1, stopped_render_test.go 1.
- board_align_test.go removed: its one test compared the board inset against the provider hub.
- Test files never copied: board_notmux, codex_grouping, default_provider, extrabody, installclaude, provider_classes, setup, teammate_events, verbose_unix, verbose_windows.
- Dropped by cause (129 tests): 41 used teammates or team history, 79 used providers, keys, forms, templates, the picker or the config dir, 9 used restart or save.
- Test helpers changed: `boardModel(t, jobs)` and `browserModel(t, jobs, runs)` lost their teammates argument and start from `NewModel()`. Pin tests use a `pinState(t)` temp `pinned.StateDir`. pinned/pinned_test.go 7 to 5 (TestKindsDoNotCollide, TestPurge).
- pinned's `validID` now allows only letters, digits, `.`, `-`, `_` (cc-fleet's ids package did the same; ds job and batch ids fit).

What T3 must replace.

- `dsjobs.ListJobs`, `ListRuns`, `ReadIO`, `ReadActivity`: read `STATE/jobs/*/job.json`, `batch.json`, `prompt.txt`, `report.txt`, `activity.jsonl`.
- `sessionProjectDir` falls back to `Result.Project` when the session cwd is unknown. Keep that or fill session meta from the job.
- Job ids render cut to 8 characters (`1122-163` for `1122-1630`, batch `b1122-13`). That is cc-fleet's short-id rule. ds ids need their full form.
- Entering a session's boxes with the cursor on a run row shows `(loading…)` in the Subagents card until the next 3 s refresh. Not checked whether cc-fleet does the same.

What T5 must replace: `StopRun`, `PurgeRun`, `DeleteJob`, `StopJob`, `DeleteSession`, `ClearFinished`, `SendLeafCommand`, `SendPhaseCommand` all return "not available yet (ticket T5)". `WithRunLock` just calls fn. `KeptWorktreeNotices` returns nil.
