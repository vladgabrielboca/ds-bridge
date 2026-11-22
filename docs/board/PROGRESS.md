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

## T3 (2026-11-22): real data for single jobs

Same branch and worktree, not committed yet.

What changed.

- `board/internal/dsjobs/read.go` replaces the stub. `ListJobs` reads `jobs/*/job.json` (skips `b*` dirs and any job with `batch`), `progress/<id>.json`, `last.txt`, `last.txt.pending` and `running/<id>.json`. Newest first by `started`.
- Status follows SPEC 5. Extra rules found while building it.
  - Progress `state: error` counts only when no result word exists yet, or when it says `interrupted` and is newer than job `ended` (+1 s). A round that ended with an error already has PROVIDER/ERROR/LIMIT in job.json, and its progress `updated` can be a fraction of a second after the int `ended`.
  - A job with a live `running/<id>.json` pid and no progress file yet shows `running`.
  - A job with no result word, no pending file and no live pid shows `stopped`, outcome `no result`.
- New `Result` fields: `Outcome` (`OK · landed`, `OK · approved`, `OK · not landed`, `rejected`, `interrupted`, the word), `Hint` (BLOCKED: the `question:` line of last.txt and `ds reply <id> …`), `Kind` (`research` when answer.txt exists or agent is `researcher`, else `run`).
- `ReadIO`: prompt is job `task` with the worker-rules and WORKSPACE preamble cut, as `ds list` does. Answer is answer.txt, else report.txt. While a round runs the answer is empty, since report.txt then holds the previous round.
- `ReadActivity`: `tool(arg)` per tool record, latest usage, torn last line skipped. Paths inside `STATE/work/<id>/` show relative (`edit(calc.py)`); a shell command keeps its `cd /abs/path`.
- Times are local (`StartedAt` in local RFC3339). Model drops the provider prefix.
- tui: full job and batch ids (`shortJobID`/`shortRunID` no longer cut), `round N` instead of `attempt N`, Overview `agent  worker · run`, Outcome uses `Outcome` and adds the `Hint` lines for failed jobs, `(no activity recorded)` for jobs without activity.jsonl, `(no prompt or output recorded)` instead of the persist-io note. Refresh cadence unchanged (3 s, 500 ms while a job runs).
- Tests: `internal/dsjobs/read_test.go` on `testdata/state` (running with the test's pid, dead pid from an exited `true`, landed, open OK round 2, BLOCKED, research, pre-T1, rejected, queued, batch child, b* dir), plus a check that reading changes nothing in the state dir. Two tui tests updated for the new texts.

Live check (job 1122-164659, scratch project with 3 bugs, board in tmux 150x40).

- While running: Activity went 2, 4, 7 tool calls and ↑ ctx 11.1k, 11.4k, 11.9k across captures 4 s apart. Output showed `(empty)`, Outcome `Still running…`.
- After: `✔ Done`, tokens `↑ 12.3k · ↓ 728`, turns 6, stop OK, Output = the worker's report block, Outcome `OK · landed · 6 turns`.

Left for later.

- Batch children (jobs with `batch`) are not shown at all until T4.
- ↑ while running is the activity usage `in` (peak context); once done it is job.json `tokens.in`. They were close in the live run (11.9k vs 12.3k), not checked on a long multi-round job.
- Session cwd for grouping comes from `sessiontitle`; the `Result.Project` fallback from T2 is kept.

## T4 (2026-11-22): batches as the workflow tree

Same branch and worktree, not committed yet. The user chose to keep the box title "Dynamic Workflows".

What changed.

- `board/internal/dsjobs/batch.go`: `ListRuns(jobs)` reads `jobs/b*/batch.json` (it now takes the refresh's job list instead of reading jobs again). Batch = run (title, owner as session, project as cwd, `started`, `ended` as UpdatedAt). Group = phase, in batch.json order.
- Phase title is the plan's step title, read best effort from the `plan` file (`## <id> <title>`, same `step`/`s` prefixes as `parse_plan`). The group label is kept in front only when it differs from the row number (`1-2 Fix a`), since the rail already shows the number. No plan file: the label alone.
- `ListJobs` now returns batch children with `RunID` = batch, `Phase` = their group's title (from `batch_group`, else the group whose `job` it is), `Label` = job id. A batch job with no group is the integration fix job.
- `RunPhase.Status` and `Detail` are new. Group `SKIPPED` → `stopped` with `skipped: needs step 2 (FAIL)` (the reason worked out like `batch.py`: the first needed group that did not pass and land). A result word other than OK → `failed` with the word. A group with `needs` shows `needs step 1`. The tui shows Detail as the first line of the phase's right pane, and the rail glyph is the status dot for done, failed and stopped phases (cc-fleet only had ✔ for done, so a failed phase used to read as ✔).
- Integration check: a last pseudo-phase `integration check`, chosen over the run header's right side because it looks like cc-fleet's phase list and gives the fix job a place as its agent. Detail is `pass · \`cmd\``, `pass (after fix job) · …`, `fail · …`, or `not run (not every step landed)` (stopped look). Only shown when the batch has a check.
- Run status: `ended` set → done, or failed if any group failed, was skipped, or the check failed. No `ended`: running while a child runs or batch.json was written in the last 30 s; otherwise failed with Description `interrupted` (ds records no batch pid, and group transitions rewrite batch.json at once).
- Finished agents show job.json `tools` (new `Result.Tools`) instead of 0. The runs box right title fits 48 characters so the batch id isn't cut.
- Tests: fixtures `b1122-100950` (3 groups: OK, FAIL, SKIPPED needing the failed one), `b1122-102000` (check passed after a fix job), `b1122-103000` (killed: no end, aged batch.json), plus `plan.md`. The T3 count and order checks now allow batch children.

Live check: `ds batch` on a scratch project, plan "Fix area code", 2 steps, step 2 `needs: 1`, check `python3 test_all.py`. Batch b1122-165227, jobs 1122-165227 and 1122-165242, all landed, integration pass, 30 s.

- At 1 s: phase 1 running (`0/1`, `↓ 199 out · 2 tools`), phase 2 and the check queued.
- At 7 s: phase 1 ✔ `↓ 517 out · 4 tools · 14s`, phase 2 `0/1` running.
- At 19 s: all three ✔. Phase 2 shows `needs step 1`; the check phase shows `pass · \`python3 test_all.py\``.

Left for later.

- The run header and runs box show no `interrupted` text for a killed batch, only the failed dot (Description isn't rendered anywhere in the ported view).
- With the cursor on the check phase the header reads `0/0 agents · ↑ 0 · ↓ 0` when no fix job ran.

## T5 (2026-11-22): stop, delete, clear, pin

Same branch and worktree, not committed yet.

What changed, Python.

- `ds forget ID...` (hidden; in `dslib/jobs.py`). One line per id, `forgot X` or `keep X: <why>`, exit 1 if any id was kept. Closed means: not running (live pid in `running/<id>.json` or a running progress file), and job.json status landed, approved or rejected, or open with its private copy dir gone. An open in-place job and an open job whose copy exists are kept with "ds approve or ds reject it first". Removal reuses `cleanup_shadow` (refs + index) and `drop_copy`, then removes progress, running marker, pin and the job dir. A batch id needs every child closed (groups' jobs, fix_job, and any job.json naming the batch) and no live batch pid; it then forgets the children and the batch dir.
- `dslib/batch.py`: batch.json has `pid`. Children run through `Popen` and are tracked. On SIGTERM (or any exit) the batch stops starting groups, SIGTERMs its live children (each child's own handler interrupts its OpenCode session), waits up to 40 s, marks queued/running groups `STOPPED`, and writes `ended` and `stopped: true`. The integration fix job goes through the same tracked path.
- `ds board` exports `DS_BIN` (the ds copy that started it), so the board's forget calls the same copy.
- Tests: 5 new in `tests/test_ds.py` (forget of a landed copy job with refs/copy/progress/pin, the keep reasons, batch kept and forgotten, and a real SIGTERM of a `cmd_batch` with a fake child).

What changed, Go.

- `board/internal/dsjobs/actions.go` replaces the T5 stubs. `isDSProcess` is the pid guard: argv is `python… …/ds <run|revise|reply|research|batch|init>`, and the process started no later than the file that names it (progress `updated`, marker mtime, or batch `started`, +2 s), from `/proc/<pid>/stat` starttime + btime. `StopJob`, `StopRun`, `SendLeafCommand` (stop one batch job) and `SendPhaseCommand` (stop a group's job, found by phase title) send SIGTERM. `DeleteJob`, `PurgeRun` and `ClearFinished` run `ds forget`. `ClearIDs` picks the session's closed, unpinned standalone jobs and batches (batch children go with their batch).
- `Result.Closed` and `WorkflowRun.Closed` (read.go, batch.go), same rule as forget. Batch: with a pid, running means the pid is a live ds batch; the 30 s mtime rule is kept only for batches without a pid. `STOPPED` group → stopped, detail `stopped from the board`; `stopped: true` → run failed, Description `stopped`.
- tui: `x` on a job row (boxes and entity level) and on a run row; prompts reworded for ds. `d` opens the confirm and shows ds forget's line (green) or the keep reason (red); a running job says "stop it first (x)". `c` counts first ("Clear N closed, unpinned job(s) or batch(es)"), red frame, result `cleared N`, plus the keep reasons if any. The sessions level had cc-fleet's "delete every record" wipe; removed, `c` there clears that session the same way. Footers list `x stop` and `c clear` where they work.
- Tests: `actions_test.go` (pid guard incl. recycled pid and non-ds argv, StopJob, StopRun, phase stop, stopped batch, Closed, ClearIDs), `TestBoard_StopJob`, the clear modal test un-skipped. Every tui test now runs with `DS_BIN` set to a fake ds, so no test can call the live one.

Live check (board in tmux 150x40, branch ds, scratch project in the session scratchpad).

- Stop: job 1122-171644 running, `x`, Confirm. The card turned `Failed`, Outcome `interrupted`; progress `state: error, last: interrupted`. OpenCode session ses_dsd07906ac99d88ac61590: `outcome: interrupted`, idle at 17:16:57 (the second of the confirm), output tokens 146 and unchanged 15 s later; no `opencode run` left.
- `d` on that interrupted job: `keep 1122-171644: open, its copy … is still there; ds approve or ds reject it first`.
- Pin: job 1122-171756 (landed) pinned with `p`, ★ shown. `landed_at` set 2 days back, `gc_landed()` run: still `landed`, both refs kept.
- `d` on it: `forgot 1122-171756`; job dir, pin, progress file and refs gone.
- `c` after rejecting 1122-171644: modal said 1 job, result `cleared 1`, job dir gone.

Left for later.

- A batch stop is tested only with a fake child (unit test), not with a live `ds batch`.
- Batches started by the main ds (no `pid` in batch.json) can't be stopped from the board; it says so.
- Until the merge, `ds board` from main has no `forget`: delete and clear only work when the board is started by the branch ds.
- Footers at 150 columns fit; narrower terminals cut them (not checked how cc-fleet handles that).
