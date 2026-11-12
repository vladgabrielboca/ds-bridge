# Benchmark: Claude alone vs Claude + ds

Answers one question with numbers: how many Claude tokens does delegating to ds save, and does the code still pass?

- **Fixture, tasks, hidden tests:** taken from [cheap-agent](https://github.com/muaaz9329/cheap-ag) (MIT, see `LICENSE-cheap-agent`): `repo/` is a tiny Node project tested with `node --test`; `tasks.json` holds eight prompts (feature, refactor, bug fix, tests, migration, docs, research, a vague one).
- **Arms:** each run copies `repo/` to `~/.local/share/ds-bridge/bench-runs/<run>` and runs the prompt with `claude -p` and a fresh session id.
  - `baseline`: delegation off, Claude works directly.
  - `ds`: `ds delegate on --session <id>` first, so the hooks, guard and planner playbook apply as in a delegating chat. One extra system line asks for ds commands in the foreground, since `claude -p` would end before a background job reports.
- **Permissions:** scoped, not `--dangerously-skip-permissions`: `--permission-mode acceptEdits` plus an allowlist of file tools and the shell commands the tasks need (`node`, `npm test`, `ds`, read-only shell and git).
- **Grading:** hidden tests are copied into `test/` after the run and the whole suite must pass. The docs task may change only `README.md`; the research task must cite `pricing.js` and rounding and leave the tree unchanged.
- **Metrics** (one line per run in `results.jsonl`): pass, Claude tokens (input + cache creation + cache read + output), cost estimate, turns, seconds, ds jobs and worker seconds from the ds ledger.

```bash
python3 bench/run.py --tasks t7 --model sonnet       # pilot: one task, both arms
python3 bench/run.py --runs 2 --model opus           # full sweep: 8 tasks × 2 arms × 2 runs
python3 bench/run.py --report                        # medians per task and arm, total saving
```

It spends real Claude usage (each run is a full Claude Code session). Few runs per cell are noisy: treat differences under ~1.5× as noise.
