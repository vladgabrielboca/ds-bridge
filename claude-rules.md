# Delegation to DeepSeek (ds-bridge): opt-in per chat

**Default: Claude works directly** (edits, builds, tests) in every chat. Delegation via `ds` is on only when I turn it on for that chat: `/ds:on` (or `/ds:muse` for the Muse Spark worker), or I say "delegate to deepseek" / "use ds" -> run `ds delegate on [--model muse]`, which prints the rules (they come back after compaction; `ds playbook` rereads them). `/ds:off` / "stop delegating" -> `ds delegate off`; `/ds:status` shows the state. If ds errors (quota, region, auth, timeout), tell me and stop; don't silently do the work yourself.
