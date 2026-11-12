# Delegation to DeepSeek (ds-bridge): opt-in per chat

**Default: Claude works directly** (edits, builds, tests) in every chat. Delegation via `ds` is on only when I turn it on for that chat: `/delegate`, or I say "delegate to deepseek" / "use ds" -> run `ds delegate on`, which prints the rules (they come back after compaction; `ds playbook` rereads them). `/delegate off` / "stop delegating" -> `ds delegate off`. If ds errors (quota, region, auth, timeout), tell me and stop; don't silently do the work yourself.
