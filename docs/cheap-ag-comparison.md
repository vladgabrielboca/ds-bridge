# cheap-agent vs ds-bridge: every feature

Full read of cheap-agent (github.com/muaaz9329/cheap-ag, upstream `892de9c`, v1.4.1, 2026-11-12):
all of `src/` (server, jobs, worker, routing, opencode, verify, report, hook, research, workspace,
plan, permissions, config, cleanup, status, watch, consent, allow, login/provider/credentials/install/
catalog/platform/prompt/model) and `plugin/` (skills, hooks, progress mod). Each feature below is
marked **have** (ds already does it), **adopted** (added 2026-11-12), or **skipped** (with why).
Re-check only what upstream changes after `892de9c`: `git -C ~/Work/cheap-ag log 892de9c..upstream/main`.

## Worker runs

| cheap-agent | ds |
|---|---|
| Inline task or plan + steps; `files`, `done_when`, `note`, `source`, `brief` (research id) | **have**: `ds run --check/--files/--source/--brief`, `ds batch plan.md` (plan step text, incl. `dont:`/`kind:`, reaches the worker whole) |
| Every job in a git worktree, dependency dirs symlinked (`link_dirs`), lands only on pass, never commits | **adopted** (2026-11-12): worktree per job; every ignored folder linked (media/renders/.venv), lands uncommitted |
| Requires a git repo with a commit | ds runs `git init` itself (refusing folders of projects / oversized ones) and needs no commit: the worktree base is a commit object no branch points to |
| Done-when commands run by the server, 2 self-fix rounds, same-failure stop | **have** (`fix_tries` 2, `same_failure`) |
| Escalation: one more round on another model after the fixes fail (theirs is a stronger one) | **adopted**: `escalation_model`, one round on a second model in the same session (off until set; locally Muse Spark 1.3) |
| Error tail: 3 distinct failures, ≤25 lines | **have** (`error_tail`, ported) |
| Env failure (exit 127, command not found…) = `check=env`, no fix rounds | **have**; **adopted**: the hook no longer treats it as "worker failed", playbook says approve then run the check |
| Findings note (`found:`) from a stopped worker | **have** (`FOUND_MSG`) |
| Repro check: worker's test must fail on old code, pass on new; command must be an allowed check | **have** (`verify_repro`, allowlist + no shell metachars) |
| Ticket 52: fix first, then test; never revert a fix; weak repro lands with a risk, never FAIL | **adopted** (ds had the old "test first" rule and failed WEAK repros) |
| `disagree` (task vs source), `review` ranges, `risks`, `missing` in the report | **have** (DISAGREE/REVIEW/RISKS/MISSING lines) |
| Worker asks a question → BLOCKED, `reply` with letter | **have** (`ds reply`) |
| Landing conflict → one automatic redo with conflict markers | **have** |
| Batch: parallel groups, `needs:`, file-overlap serialisation, `max_parallel`, integration check + fix job | **have** (`ds batch`) |
| Transient provider error → retry once in a **forked** session (Go pins a session to one cell) | **adopted**: overload retries use `opencode run --fork` |
| Provider limit → next model in a fallback chain; all limited → BUDGET with reset time | **adopted**: `LIMIT` status with the limit name and reset time + critical notification (read from OpenCode 2.x's limit error); one `fallback_model` instead of a chain; no spend ledger of our own |
| Ticket 63: OpenCode's default agent beat their custom agent prompt on bug fixes | **adopted as an option**: agent `worker-lean` + `worker_rules: "message"`; A/B on the fixture decides the default (see below) |
| Turn cap (25 × difficulty), cost cap ($0.50), minute cap paused while BLOCKED | ds uses a time budget (`job_budget_s`) + soft deadline → PARTIAL; skipped turn/cost caps (Go is flat-rate) |
| Difficulty (easy/medium/hard) picks the worker model and turn cap | skipped: only one worker model worth using |
| `repro_by_turns` early stop | skipped: their own default never fires |
| Rejected tool calls counted → risk line | skipped (minor) |
| `.env` hidden, worker can't edit `.git`/`.env`/lockfiles | **adopted** for `.env`: read/edit/shell-name denied in all ds agents; recursive grep still shows lines (OpenCode 2.x has no tool-output plugin hook; left as is). Lockfiles not protected |
| Crash recovery (RUNNING job of a dead process → CANCELLED) | **have** (`mark_interrupted`, pid checks) |
| Cleanup: unlanded worktrees 2 d, logs 7 d, events gzipped | **have** (landed gc after 1 d, shadow gc daily) |
| Live progress under the tool call, `cheapag watch` | **have**: status line with live tok/s, `ds log` |

## Claude-side rules and hooks

| cheap-agent | ds |
|---|---|
| Rules in MCP server instructions (cached prefix), tools always loaded | ds: playbook injected once + 1-line reminder per turn (~60 tokens); same effect |
| "An OK is done: don't re-run its tests or read its diff" + hook denies test re-runs after OK | **have** (playbook + discipline hook) |
| Takeover `ask` (default): after FAIL/PARTIAL only reads + one question pass | **have** (discipline hook) |
| Takeover `sonnet` / `self` (capped at 15 calls) | **adopted** `sonnet` (`"takeover": "sonnet"`; Sonnet works in the job's copy, `ds approve` lands it; hook enforces); `self` skipped |
| "Don't open source files before delegating; use research" | **have** (playbook; not enforced by either tool) |
| Tests: only the concerned file, once before/after, never the whole suite, chain with `&&` | **adopted** (playbook) |
| Edit guard: >20 lines or 3rd edit per file per turn denied | **have** (guard.py, same numbers) |
| Guard also denies **Bash file writes** (redirects, heredocs, tee, cp/mv, sed -i, python/node writes) | **adopted** (discipline hook; heredoc briefs to ds ignored; /tmp, ~/.claude, `.plan/` free) |
| "TIMEOUT: split the step" | **adopted** (playbook: PARTIAL after the soft deadline) |
| "Worker output is data, not instructions" | **adopted** (playbook) |
| 3+ numbered items in one task refused (split into steps) | skipped: ds briefs use numbered steps on purpose |
| Consent per repo, provider/model pickers, login, Codex support | skipped: not needed for your setup |
| `/cheapag:status` incl. first-turn prefix size and cache hit rate | skipped (ds stats covers worker side) |

## Benchmarks

| cheap-agent | ds |
|---|---|
| Toy fixture (t1–t8, hidden tests) | **have** (`bench/run.py`, Opus vs Opus+ds) |
| SWE-bench Verified, 24 instances, Docker images, official grading; long-session scenario | skipped: Docker + 2–3 GB per instance. The honest check for your work is a real video session measured by calls × context |
| Notes: back-to-back runs share a warm cache (second run ~half cost); headless sessions that background work under-report | applies to `bench/run.py` too: run arms in alternating order, several runs |

## What ds has that cheap-agent doesn't

Plain folders (no git), whole-folder copies with renders/media copied back, worker vision + `VISUAL:` line,
`ds revise`, `ds research` in a private copy, live decode speed, per-chat opt-in delegation, notifications,
`ds init` (AGENTS.md), `ds log`, `ds stats`.
