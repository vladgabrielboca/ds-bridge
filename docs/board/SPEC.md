# ds board: spec

A live board for ds-bridge workers, ported from cc-fleet's Agents Board and changed only where ds-bridge differs. Run it as `ds board` in a tmux split or a second terminal next to Claude Code.

Written 2026-11-22. Source of the port: `~/Work/cc-fleet` (github.com/ethanhq/cc-fleet, commit `29262a6`, 2026-10-24, Apache-2.0).

## 1. Goal

While a ds job runs, the user can see which jobs exist, what each worker was asked, which tools it is calling right now, how many tokens it used, and its final output. Today the only live view is the one-line status line (`statusline.sh`), and the full transcript is only available after the job with `ds log JOB`.

The board must look and behave like cc-fleet's Agents Board (`ccf` → Agents Board). Where something has no ds equivalent it is dropped, not redesigned. Any redesign needs the user's OK first.

## 2. Reference material (read these, don't guess)

- Demos, frame by frame: `~/Work/cc-fleet/docs/assets/demo-subagent.webp`, `demo-tui.webp`, `demo-workflow.webp`. Extract frames with `magick X.webp -coalesce f_%03d.png` and look at them.
- Stills: `docs/assets/subagent-board.webp`, `workflow-board.webp`, `workflow-leaf.webp`.
- Board code: `~/Work/cc-fleet/internal/tui/` (model.go 4683 lines, view.go 3438, confirm.go 390, browser.go 548, pin.go 72, workflow_events.go 114, teammate_events.go 252, screen_handlers.go 63).
- Data it reads: `internal/subagent/types.go` (`Result`), `job.go` (`ListJobs`, `jobMeta`), `activity.go` (activity sidecar format), `internal/sessiontitle/` (Claude session titles + cwd), `internal/pinned/` (pin registry).
- ds-bridge side: `dslib/runner.py` (`Tally`, `write_progress`, `stop_worker`, `cleanup_progress`, `mark_interrupted`), `dslib/jobs.py` (job record at line ~244, detach), `dslib/batch.py`, `dslib/results.py` (`gc_landed`, `record_ledger`), `dslib/info.py` (`cmd_list`, `cmd_status`, `cmd_log`), `.claude/notes/ds-bridge-history.md` (pitfalls).

## 3. What the cc-fleet board is (verified in code and demos)

Levels inside one screen (`asMode` in model.go):

1. Projects: one row per project dir, with counts.
2. Sessions: the Claude Code sessions in that project (title from the Claude transcript, via `sessiontitle.ResolveMeta`).
3. Boxes: three stacked boxes for the session: Dynamic Workflows, Agent Teams, Subagents. Each lists its rows with a status dot.
4. Entity: left rail = the rows of one box, right = the detail card of the selected row.
5. Run drill (workflows only): phases → agents → agent card.
6. Browser (`ctrl+f`): flat, newest-first list of past jobs/runs, type to filter.

Job detail card (`jobDetailLines`, view.go:2682), in this order:

- status word + dot, model, attempt if > 1
- Overview: job id (own line), profile, started, elapsed, tokens `↑ in · ↓ out`; when finished also cost `~$X est`, turns, stop reason
- Prompt: collapsed to 4 display lines with `Prompt · N lines · ⏎ expand`
- Activity: `Activity · last 3 of N tool calls`, then the last 3 signatures like `Read(/path)`, or `(no tool calls)`
- Output: the final answer file, `(empty)` while running
- Outcome: `Still running…`, `done · N turns`, `stopped`, `held — …`, or the error code in red

Live data: the job writes an NDJSON activity sidecar (`activity.go`, record `{seq, kind: tool|usage, tool, arg, in, out, cache, ts}`; arg is the first input value, masked, max 200 chars). The board re-reads it (`readLeafActivity`, workflow_events.go). Refresh: full board every 3 s, workflow-only every 500 ms while something runs.

Keys (footer, view.go:618): `↑/↓` row, `→/⏎` open, `←/esc` back, `j/k` scroll card, `⏎` expand prompt, `p` pin, `d` delete, `c` clear finished, `x` stop, `r` restart (workflow), `R`/`r` refresh, `ctrl+f` find, `q` quit. Destructive actions open a centered confirm modal (confirm.go), cursor starts on Cancel.

Colors: `lipgloss.AdaptiveColor` pairs of ANSI 256 indices, light/dark picked from the terminal background.

## 4. Mapping to ds-bridge

| cc-fleet | ds board | Notes |
|---|---|---|
| Subagent job (`subagent.Result`) | ds job (`jobs/<id>/job.json`) | the main case |
| Dynamic Workflow run → phases → agents | ds batch `b<id>` → groups → jobs | needs a batch manifest (T1) |
| Agent Teams box, tmux panes, inboxes | dropped | no ds equivalent |
| Lead session id (`lead_session_id`) | job `owner` (`CLAUDE_CODE_SESSION_ID`) | empty owner = "(no session)" group, like theirs |
| Project dir (session cwd) | job `project` if set, else `dir` | `dir` is the private worktree for isolated jobs; group by the real project |
| Session titles | same: port `sessiontitle` as is | reads `~/.claude/projects` |
| Model Providers hub, add/edit/keys, Codex OAuth, setup and install nudges, `ccf run`, self-update | dropped | ds config stays in `~/.config/ds-bridge/config.json` |
| profile (slim/full) | agent (`worker`, `researcher`, `worker-lean`) + kind (run/research/init) | same Overview slot |
| attempt | round (revise count) | show `round N` when > 1 |
| status queued/running/held/done/failed/stopped | see section 5 | |
| cost `~$X est` | `Tally.cost` summed over rounds | show only if > 0 (OpenCode Go may report 0) |
| turns | steps | |
| stop reason | ds result word (OK, FAIL, BLOCKED, CONFLICT, LIMIT, PARTIAL, …) | first word of `last.txt` |
| Output (`.answer` file) | the worker's final report text (`report.txt`, new in T1); `answer.txt` for research | |
| `r` restart leaf/phase | dropped | ds has revise, which needs a message from Claude |
| `x` stop | SIGTERM to the ds process (pid in progress file) | ds's handler already interrupts the OpenCode session |
| `d` delete, `c` clear | allowed only on closed jobs (landed / approved / rejected / failed with no open worktree) | an open job owns a worktree; deleting it would orphan it |
| `p` pin | pin registry, honored by ds's own GC | `gc_landed` deletes landed jobs after 1 day today |
| `s` save workflow | dropped | |
| `h`/`s` hide/show pane | dropped | |

## 5. Status model

Board status of a job is derived, in this order:

1. progress file `progress/<id>.json` has `state: running` and its pid is alive → `running`.
2. `state: running` but pid dead → `failed` with outcome `interrupted` (same rule as `cleanup_progress`).
3. job `status` is `landed` or `approved` → `done`, outcome shows the result word and "landed"/"approved".
4. job `status` is `rejected` → `stopped`-style neutral, outcome "rejected".
5. job `status` is `open` and the result word is OK → `done` (waiting for approve), outcome "OK · not landed".
6. result word FAIL / LIMIT / CONFLICT / BLOCKED / PARTIAL / PROVIDER / ERROR (the full set is whatever `dslib/results.py` emits; check it, don't trust this list), or progress `state: error` → `failed` (red), outcome = the word. BLOCKED shows the question line from `last.txt` and the hint `ds reply JOB …`.
7. `last.txt.pending` exists and no progress yet → `queued`.

Batches: running if any child runs; failed if any child failed or was SKIPPED; done when all children are done.

## 6. Data contract (Python writes, board reads)

All under `~/.local/share/ds-bridge` (`STATE`). The board takes `--state DIR` (default that path) so tests can point it at a fixture.

Existing, unchanged: `jobs/<id>/job.json`, `jobs/<id>/last.txt`, `jobs/<id>/last.txt.pending`, `jobs/<id>/answer.txt` (research), `jobs/<id>/full.diff`, `progress/<id>.json`.

New (ticket T1):

- `jobs/<id>/activity.jsonl`: one JSON object per line, same shape as cc-fleet's activity sidecar so `readLeafActivity` ports with only a path change:
  - `{"seq": n, "kind": "tool", "tool": "read", "arg": "<shell command, else path / filePath / pattern; empty for other tools; max 200 chars, secrets masked>", "round": r, "ts": "RFC3339 UTC, Z suffix"}`
  - `{"seq": n, "kind": "usage", "in": <peak context = input + cache_read of the latest step>, "out": <cumulative output + reasoning for the whole job, across rounds>, "cache": <cache_read of the latest step>, "round": r, "ts": …}`
  - Appended from `Tally.feed` as events arrive, one open/append/close per line, never blocking the run. Kept across revise rounds (the `round` field separates them).
  - Masking: port the key-like patterns of cc-fleet `internal/redact/redact.go` (`MaskKeyLikeString`) to Python and apply them to every arg, then truncate to 200 chars. For tools that write files (edit, write, patch) the arg is the file path only, never the content.
- `jobs/<id>/report.txt`: the worker's final text for the round (what `parse_report` reads), overwritten each round.
- job.json gains, at the end of each round: `steps`, `tools`, `cost`, `tokens` `{in, out, cache}` (all four summed over rounds), `result` (the first word of the block), `ended` (epoch, int). Research jobs get them too (result OK / ERROR / PROVIDER / LIMIT). A batch child also has `batch_group` (its group index) next to `batch`.
- `jobs/b<id>/batch.json`: `{id, title, plan, project, owner, started, groups: [{label, steps: [ids], needs: [group indexes], job: <job id or null>, status}], check, ended, integration, fix_job?}`, rewritten atomically under `batch.json.lock` whenever a group starts, ends or is SKIPPED. Group `status`: `queued`, `running`, then the child's result word or `SKIPPED`. `job` is set by the child as soon as its job exists. `needs` lists only `needs:` dependencies (file-overlap ordering is not in it). `ended` is null until the batch ends; `integration` is `none` / `pass` / `fail` / `pass (after fix job)`; `fix_job` only when an integration fix job ran.
- Pins: `pins/<id>` zero-byte files. `gc_landed` and any other ds cleanup skip pinned jobs. Delete from the board removes the pin.

## 7. Go program layout

```
board/
  go.mod                 module ds-bridge/board, Go 1.27, deps: bubbletea v1.3.10, bubbles v0.21.0, lipgloss v1.1.0, x/ansi, x/term (same versions as cc-fleet)
  main.go                flags --state DIR, --version; runs tui.Run
  LICENSE-cc-fleet       Apache-2.0 text copied from ~/Work/cc-fleet/LICENSE
  NOTICE                 "Derived from cc-fleet (github.com/ethanhq/cc-fleet) commit 29262a6, Apache-2.0. Modified for ds-bridge."
  internal/tui/          ported board files, each keeps a header comment: "Derived from cc-fleet internal/tui/<file> (Apache-2.0); modified."
  internal/dsjobs/       the data layer: reads section 6 files into a Result-like struct, batches into run groups, activity snapshots, pins
  internal/sessiontitle/ copied from cc-fleet with homedir inlined
```

Porting rule: keep cc-fleet's types and function names where the board uses them, so diffs against upstream stay readable. `dsjobs.Result` carries the fields the tui reads from `subagent.Result` (OK, Model, DurationMs, NumTurns, StopReason, Usage, CostUSD, SessionID, LeadSessionID, RunID, Phase, Label, Attempt, PromptProfile, JobID, Status, PID, StartedAt, ErrorCode, ErrorMsg), filled from ds files.

`ds board` (Python, `dslib/cli.py`) execs `~/.local/bin/ds-board` with the same args. `install.sh` runs `go build -o ~/.local/bin/ds-board ./board` when `go` is on PATH and prints a one-line skip note otherwise.

## 8. Actions and safety

- `x` stop: only for `running`. Check that the pid's `/proc/<pid>/cmdline` is a python process running ds (`DS_BIN` path) before signalling, like cc-fleet's `cmdlineIsClaudeJob`. Send SIGTERM. Confirm modal first.
- `d` delete: only closed jobs (section 4 table). Calls `ds` itself (new hidden command `ds forget JOB`, T5) so the Python side owns all deletions of job dirs, shadow refs and worktrees. Never `rm -rf` from Go.
- `c` clear: deletes every closed, unpinned job of the focused session via the same command. Confirm modal lists the count.
- `p` pin / unpin: writes or removes `pins/<id>`.
- The board never approves, rejects or revises. Those stay Claude's decisions.

## 9. Out of scope

- Agent Teams, tmux panes, provider management, Codex, Windows, macOS, self-update, onboarding, saved workflows, restart.
- Streaming the worker's text into the Output section while it runs (cc-fleet doesn't). Possible later as ticket T7.
- A Hyprland key binding to open the board. Design it with the user first (left-hand keys only, check `hyprctl binds -j`).

## 10. Open questions for the user

1. Colors: keep cc-fleet's ANSI 256 pairs (default), or switch to the 16 base ANSI colors so the board follows the Omarchy theme.
2. T7 (live worker text in Output): wanted or not.

## 11. Tickets

One fresh Claude session per ticket, started in `~/Work/ds-bridge`. Order matters.

All tickets work on one branch, `board`, in a git worktree at `~/Work/ds-bridge-board`, created from `main` by the first ticket that runs. Start each ticket session in that folder. `~/Work/ds-bridge` stays on `main`, because `~/.local/bin/ds` is a symlink into it and every Claude chat runs that copy. Inside the worktree, run the branch's code as `~/Work/ds-bridge-board/ds`, and never run `./install.sh` there, since it would re-point the symlink. Nothing goes to `main` until T6 is done and the user OKs the merge.

| # | File | Depends on | Size |
|---|---|---|---|
| T1 | `tickets/T1-python-data.md` | none | Python only |
| T2 | `tickets/T2-go-scaffold.md` | none (can run before or after T1) | copy + strip Go |
| T3 | `tickets/T3-jobs-board.md` | T1, T2 | data layer + jobs path |
| T4 | `tickets/T4-batches.md` | T3 | batch tree |
| T5 | `tickets/T5-actions.md` | T3 | stop, delete, clear, pin |
| T6 | `tickets/T6-finish.md` | T4, T5 | browser, polish, docs, live test |
| T7 | `tickets/T7-live-output.md` | T6, user OK | optional |

Progress and hand-off notes go in `docs/board/PROGRESS.md`. Each ticket ends by updating it.
