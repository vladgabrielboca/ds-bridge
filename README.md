# ds-bridge

Claude plans and reviews, DeepSeek types. A cheap OpenCode worker for Claude Code that returns a summary and a diff, not files.

## Why

Claude usage is expensive, but delegating only saves money if the orchestrator doesn't re-read everything afterwards. ds-bridge keeps Claude's input to a ≤150-word report plus a diff the bridge computes itself — it doesn't trust the worker's account of what it changed — capped at 250 lines.

Measured: reviewing a job costs Claude roughly 600–900 tokens. A revision round shows only that round's delta, so iterating stays cheap too.

This README, the installer and the live-progress feature were written by the worker through ds-bridge itself, then reviewed by Claude.

## How it works

```mermaid
flowchart LR
    subgraph CS["Claude side"]
        U["User"] --> CC["Claude Code<br/>plan · review"]
        SL["status line"]
    end
    subgraph DS["ds-bridge"]
        RUN["ds run"]
        SNAP["shadow git snapshot"]
        PROG["progress.json"]
        DIFF["diff vs snapshot"]
        OUT["compact report"]
    end
    subgraph WS["Worker side"]
        OC["OpenCode worker agent<br/>DeepSeek v4.1 Flash"]
        FILES["project files"]
    end
    CC --> RUN
    RUN --> SNAP
    SNAP --> OC
    OC -->|"edits"| FILES
    OC -.->|"event stream"| PROG
    PROG -.-> SL
    FILES --> DIFF
    DIFF --> OUT
    OUT --> CC
```

```mermaid
sequenceDiagram
    participant C as Claude
    participant D as ds
    participant S as Shadow git
    participant O as OpenCode worker
    C->>D: run brief
    D->>S: snapshot
    D->>O: opencode run --agent worker
    loop tool calls
        O-->>D: JSON event stream
        D->>D: write progress.json
    end
    D->>D: run check
    loop check fails, tries left
        D->>O: failure tail, same session
        O-->>D: JSON event stream
        D->>D: run check
    end
    D->>S: diff vs snapshot
    D-->>C: report + diff
    alt approve
        C->>D: ds approve JOB
    else revise
        C->>D: ds revise JOB
        D->>S: round snapshot
        D->>O: same session
        O-->>D: JSON event stream
        D->>S: diff this round only
        D-->>C: report + delta diff
    else reject
        C->>D: ds reject JOB
        D->>S: restore snapshot
    end
```

**What Claude reads vs what stays with the worker**

```mermaid
flowchart LR
    subgraph W["Worker side"]
        W1["file reads, tool output, reasoning, test logs<br/>tens of thousands of tokens"]
    end
    subgraph C["Claude side"]
        C1["STATUS / SUMMARY / CHECKS / NOTES<br/>plus the diff<br/>hundreds of tokens"]
    end
    W1 -.->|"report and diff only"| C1
```

## Live progress

Claude Code's status line can poll ds-bridge once a second (`refreshInterval: 1`) and render the worker's current step:

```
⠴ DeepSeek 1104-152450 r1 · 0:05 · step 2 · 4 tools · 144 tok · ▶ 162 tok/s · ▸ read notes.txt
⠙ DeepSeek 1104-152510 r1 · 0:02 · step 1 · 1 tool · 18 tok · ⏸ 6s · ▸ bash sleep 6
⠹ DeepSeek 1104-152510 r1 · 0:04 · step 1 · 1 tool · 18 tok · … working · ▸ bash sleep 6
✓ DeepSeek 1104-152450 done in 0:11
DeepSeek idle · 4 jobs today · ≈38k tok saved
```

Each active job gets its own line, up to four; when nothing is running the line shows today's ledger instead. Every chat sees only its own jobs: ds records the Claude Code session that ran (or last revised) a job, and jobs from other chats collapse into one `+N DeepSeek jobs in other chats` line while they run. Jobs started outside Claude Code show in every chat. A run or revise that lasts at least `notify_min_s` seconds also sends a desktop notification with the job id, project, duration and check result.

There is no percentage. An agent can't know its step count in advance, so the line shows elapsed time, the step reached so far, and the last tool call instead of inventing a progress bar.

While a job runs, ds also reads the OpenCode service's event stream (`~/.local/state/opencode/service.json`). The status line shows the live generation rate (`▶ N tok/s`) while the model writes, `✎ tool Ns` while tool arguments are being generated, an elapsed tool timer (`⏸ Ns`) while a tool runs, and `… working` when no output is streaming (waiting for the first token, or the model writing tool arguments, which OpenCode does not stream). The live rate uses only active generation time in the last five seconds, calibrated against completed steps; long pauses and tool execution do not lower it. If the stream is unavailable, no live rate is shown.

The final report's `tok/s` is decode speed: output tokens per second while the model is writing, measured from the event stream between the first and last generation event of each step. It excludes time to the first token and tool execution. The same measurement gives the time to first token (TTFT), shown after the rate as `· first token X.Xs`. When the event stream was unavailable, or no step completed, ds prints no speed rather than estimating one from `opencode run` stdout.

## Commands

| Command | Description |
| --- | --- |
| `ds run -C <dir> "<task>"` | Start a job: snapshot, run the worker (and optional check/fix loop), print report + diff. |
| `ds init [-C <dir>] [--refresh]` | Start a job that writes or refreshes `AGENTS.md` at the project root. |
| `ds revise JOB "<points>"` | Send numbered feedback in the same session; shows only this round's diff. |
| `ds diff JOB [PATH…]` | Re-show the diff, optionally `--full` or for specific paths. |
| `ds approve JOB` | Mark the job accepted; the changes stay in place. |
| `ds reject JOB [--force]` | Undo only the job's own file changes; files changed since the job are skipped unless `--force`. |
| `ds list` | List recent jobs with status and round. |
| `ds stats [--days N \| --all]` | Summarise the ledger: jobs, rounds, worker tokens, Claude's reads and estimated savings. |
| `ds log JOB [--tools-only]` | Print a readable transcript of the job's OpenCode session. |
| `ds delegate on\|off\|status [--session ID]` | Per-chat delegation mode (session defaults to `CLAUDE_CODE_SESSION_ID`). |

Flags: `-C DIR` sets the project directory (`run` and `init`), `-m MODEL` overrides the model for one job, `-f FILE` attaches a file to the brief (repeatable), `--paths P…` limits the job's file scope, `--timeout S` overrides `job_budget_s` for this invocation (`run`, `init`, `revise`), and `--check CMD` / `--fix-tries N` control the acceptance check (also on `revise`).

### `--check` and the fix loop

`ds run -C <dir> --check "pytest -q" "<task>"` runs CMD with `bash -c` in the project after the worker's first answer. If it exits non-zero (or hits `check_timeout_s`), the bridge sends the last 60 lines of output back into the same session and asks for a fix, then runs the check again, at most `fix_tries` times. The report gets a `--- check` section with PASS/FAIL and the number of auto-fix rounds, and the diff includes every change from those rounds.

### Soft deadline

A worker call that passes `soft_timeout_frac × timeout_s` (default `0.8`, so 24 minutes of a 30-minute run) is stopped and the same session is asked for its final report, including anything unfinished. That wrap-up call has its own limit, `wrapup_s` (default `180`). The `--check` still runs afterwards, and the report starts with `SOFT DEADLINE HIT`. This applies to `ds run`, `ds init` and `ds revise`, including auto-fix rounds. Both the hard timeout and the soft deadline interrupt the session through the OpenCode service, not just the `opencode run` client, so the worker stops running tools; the wrap-up call then reaches an idle session. ds chooses the session id itself before the worker starts, so interrupt works from the first second.

### Provider overload

OpenCode already retries a model request for about two minutes when the provider answers "overloaded" or rate-limited. If it still gives up, ds waits (`overload_wait_s`, default 30 s then 90 s, at most `overload_retries` times) and continues the same session, asking the worker to pick up where it left off. The status line shows `provider overloaded · retry N/M` while waiting, the report gets a `NOTE: provider overloaded` line, and a retry is skipped when the job's time budget can't fit it.

### Job time budget

`job_budget_s` (default `1800`) caps a whole `ds run`, `ds init` or `ds revise` invocation, including check runs and auto-fix rounds; `--timeout S` overrides it for one invocation. Each worker call gets `min(timeout_s, remaining)` and each check gets `min(check_timeout_s, remaining)`; the soft-deadline fraction applies to that per-call limit. If fewer than 60 seconds remain before a fix round, the round is skipped and the `--- check` section says `budget exhausted, N fix rounds skipped`. The budget is mentioned only when it is hit.

### Cleanup on approve and reject

`ds approve` and `ds reject` drop the job's refs under `refs/ds/<job>/` from the shadow repository and remove its temporary index, so old snapshots do not accumulate. At most once per 24 hours per shadow repo they also start a detached `git gc --prune=1.hour.ago` (never `--prune=now`, which could race a concurrent job). Both commands return immediately. Afterwards `ds diff`, `ds approve` and `ds reject` on that job exit with a one-line "job is approved/rejected" message instead of a git error.

### `ds reject` and `--force`

`ds reject JOB` restores only the files that the job itself changed. It compares the current working tree with the job's end-of-round snapshot: unchanged-since-then files go back to the pre-job state, while files edited afterwards (or by a later approved job) are left alone and listed as skipped. `ds reject JOB --force` restores those too. Directories left empty by removing files the job added are deleted, stopping at the project root. Jobs created before end-of-round snapshots existed still restore the whole scope, after a warning.

### `--paths` and parallel jobs

`--paths P…` limits the snapshot, diff and reject scope to those paths relative to `-C`. Independent subtasks can run in parallel: start several `ds run ... --paths <their files>` calls in the background, then review each report as it arrives. Jobs whose paths overlap a running job in the same project are refused. `ds revise` keeps the job's original scope.

With `--paths`, the bridge also records a cheap path/size/mtime listing of the whole project before the run and compares it afterwards. Files outside the job's paths that were created, changed or deleted appear in a `--- WARNING: changes outside --paths` section (up to 20 lines). Paths matching `outside_ignore` are skipped. Nothing is recorded or printed when `--paths` is not used.

Snapshots skip media, build output, big files and dependency/cache directories, so those changes are not in the diff and `ds reject` cannot restore them. To make that visible, each run and revise also compares a cheap listing of excluded files (the job's `--paths` when set, otherwise the whole project; `node_modules`, `.venv`, `venv`, `__pycache__`, `.git` and `.cache` are never entered). When something changed, one compact line is printed, for example `--- NOTE not in diff, not undoable by reject: build/ 12 changed 2 created · media/ 3 created · model.bin 1 changed`. At most five top-level groups are shown plus `+N more groups`. Set `excluded_note` to `false` to disable it.

### `ds init` and AGENTS.md

`ds init -C <project>` is a normal job (snapshot, worker, report, approve/revise/reject) with a built-in task: explore the project and write `AGENTS.md` at the root, at most 40 lines of facts about layout, build/test/lint/run commands, conventions and things not to touch. It is scoped with `--paths AGENTS.md`. If `AGENTS.md` already exists it refuses unless you pass `--refresh`, which asks the worker to update the existing file and keep correct lines. OpenCode loads `AGENTS.md` automatically, so later briefs can be shorter.

### Delegation mode (opt-in per chat)

By default Claude works directly; delegation is switched on per chat. Type `/delegate` in Claude Code (the skill in `skills/delegate`, linked into `~/.claude/skills` by the installer) or say "delegate to deepseek", which runs `ds delegate on`; `/delegate off` or `ds delegate off` ends it. While it's on, a `UserPromptSubmit` hook (`hooks/delegate.py`) reminds Claude every turn to send all execution through `ds run ...` started in the background, and the guard blocks direct edits even with `guard_allow`, `guard_mode: "warn"` or `guard-off`; only `~/.claude` and `/tmp/claude-*` stay writable. The marker lives in `~/.local/state/ds-bridge/delegate/<session_id>` (from `CLAUDE_CODE_SESSION_ID`), and stale markers are removed after 30 days.

### Guard hook

`install.sh` adds a Claude Code `PreToolUse` hook (`hooks/guard.py`) for `Edit`, `Write`, `MultiEdit` and `NotebookEdit`. Direct edits to project files are blocked with a message to delegate through `ds`; `~/.claude/`, `/tmp/claude-*` scratchpads and globs in `guard_allow` are allowed. Set `guard_mode` to `"warn"` to warn instead of blocking: the edit is not blocked by the hook, you see a warning, and Claude gets a reminder about the delegation rule. Pause it with `touch ~/.config/ds-bridge/guard-off` or `DS_GUARD_OFF=1`. It can't stop edits made through Bash (heredocs, sed), so it is a guardrail, not a sandbox.

### Stats and log

Every report appends one line to `~/.local/share/ds-bridge/ledger.jsonl`. `ds stats` prints jobs (distinct ids), rounds (ledger rows), worker tokens in/cache/out, what Claude read, an estimated saving, and (when known) the median decode speed and TTFT across rounds:

> saved = worker input + 0.1 × worker cache reads + worker output − Claude's read characters / 4

Plainly: the input-equivalent tokens the worker spent doing the job (cache reads counted at a tenth) minus the tokens Claude used reading the compact report and diff. It is an estimate, not a measurement. `ds log JOB` exports the session; `--tools-only` hides text and shows the tool calls.

## Install

Requirements:

- Claude Code with status-line `refreshInterval` support.
- OpenCode 2.x with a DeepSeek-capable provider — for example an OpenCode Go subscription, model `opencode-go/deepseek-v4.1-flash`.
- `python3`, `git` and `jq` (used by the status line).

```bash
git clone https://github.com/vladgabrielboca/ds-bridge.git
cd ds-bridge
./install.sh
```

The installer merges the `worker` agent into `~/.config/opencode/opencode.json` (backing it up first), links `ds` into `~/.local/bin`, adds the status line, the `PreToolUse` guard hook and the `UserPromptSubmit` delegation hook to `~/.claude/settings.json`, and links the `/delegate` skill. It never edits `CLAUDE.md` itself, so finish by adding the rules in `claude-rules.md` to `~/.claude/CLAUDE.md` (or a project `CLAUDE.md`). It creates `~/.config/ds-bridge/guard-off`, so the guard only acts in chats with delegation mode on; delete that file to guard every chat.

`./install.sh --uninstall` removes the worker agent, the symlink, the status line and the guard hook, touching only what ds-bridge created.

> OpenCode Go DeepSeek models need the workspace region set to **Global** in its Privacy settings.

## Configuration

Optional `~/.config/ds-bridge/config.json` overrides these defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `opencode-go/deepseek-v4.1-flash` | Model used by the worker. |
| `agent` | `worker` | OpenCode agent to run. |
| `max_diff_lines` | `250` | Lines of diff shown to Claude before truncation. |
| `max_report_chars` | `1500` | Cap on the worker's report (about 150 words). |
| `timeout_s` | `1800` | Per-run timeout. |
| `soft_timeout_frac` | `0.8` | Fraction of `timeout_s` after which the worker is stopped and asked to wrap up. |
| `wrapup_s` | `180` | Time limit for the wrap-up call that writes the final report. |
| `max_files` | `20000` | Refuse to snapshot directories with more tracked files. |
| `big_file_mb` | `5` | Files larger than this are excluded from snapshots. |
| `fix_tries` | `2` | Auto-fix rounds after a failed `--check` (override per job with `--fix-tries`). |
| `overload_retries` | `2` | Times ds continues the session after the provider stays overloaded. |
| `overload_wait_s` | `[30, 90]` | Seconds to wait before each overload retry. |
| `check_timeout_s` | `600` | Timeout for one `--check` command. |
| `job_budget_s` | `1800` | Time budget for a whole `run`/`init`/`revise` invocation (override with `--timeout`). |
| `excluded_note` | `true` | Print the one-line note about changes snapshots cannot see. |
| `notify` | `true` | Send a desktop notification when a run or revise finishes. |
| `notify_min_s` | `15` | Don't notify for jobs shorter than this many seconds. |
| `outside_ignore` | `[".git", "__pycache__", "media*", "*.log", "node_modules", ".venv"]` | Globs ignored by the `--paths` outside-change warning. |
| `guard_allow` | `[]` | Globs of paths the guard hook always allows (for example `~/work/sandbox/*`). |
| `guard_mode` | `"block"` | Guard hook behaviour for blocked paths: `"block"` (message to stderr, exit 2) or `"warn"` (allow the edit, show a warning to you and add context for Claude). |

## Safety

The worker's `bash` permission denies `git commit`, `git push`, `git reset`, `git checkout` and `sudo`. `ds` refuses to run in `$HOME` or `/`. Snapshots live outside your repository in `~/.local/share/ds-bridge/shadow`, so your own `.git` is never touched. Media and big files, `node_modules/`, virtualenvs, build output and caches are excluded; the `--- NOTE` line lists changes to those when it can see them. `ds reject` restores only the job's own changes; use `--force` for files changed after the job. Approve and reject remove the job's shadow refs (and occasionally gc) so snapshots do not pile up.

## Limitations

- No real percentage progress — the status line counts steps, not total work.
- Files over 5 MB aren't snapshotted, so `ds reject` can't restore them.
- The worker's quality is a flash model's; review is mandatory.
- Token savings from `ds stats` are an estimate, not a measurement.
- The guard hook covers Claude's edit tools only; it can't see Bash-based edits.
- Auto-fix rounds restart the status-line timer for each worker call.

## License

MIT. This project is not affiliated with Anthropic, DeepSeek or OpenCode.
