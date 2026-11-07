#!/usr/bin/env bash
# ds-bridge installer: wires the worker agent, the ds CLI and the status line
# into OpenCode and Claude Code. Idempotent; safe to re-run.
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

OC_DIR="$HOME/.config/opencode"
OC="$OC_DIR/opencode.json"
OC_BAK="$OC.bak-ds-bridge"
AGENT_JSON="$DIR/opencode-agent.json"
CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/ds"

say() { printf '%s\n' "$*"; }
warn() { printf 'install.sh: %s\n' "$*" >&2; }

require_commands() {
    local missing=()
    local cmd
    for cmd in opencode python3 git jq; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        warn "missing required command(s): ${missing[*]}"
        warn "install them, then re-run ./install.sh"
        exit 1
    fi
}

# Replace the __DS_BRIDGE_DIR__ placeholder in every string of a JSON tree.
py_merge_agent() {
    DIR="$DIR" AGENT_JSON="$AGENT_JSON" OC="$OC" python3 - <<'PY'
import json, os, sys

d = os.environ["DIR"]
src = json.load(open(os.environ["AGENT_JSON"]))
path = os.environ["OC"]

def sub(o):
    if isinstance(o, str):
        return o.replace("__DS_BRIDGE_DIR__", d)
    if isinstance(o, list):
        return [sub(x) for x in o]
    if isinstance(o, dict):
        return {k: sub(v) for k, v in o.items()}
    return o

agent = sub(src["agent"])

if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except (ValueError, OSError) as e:
        sys.exit(f"install.sh: cannot parse {path}: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"install.sh: {path} is not a JSON object")
else:
    cfg = {"$schema": "https://opencode.ai/config.json"}

cfg.setdefault("agent", {})
cfg["agent"].update(agent)

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
PY
}

py_remove_agent() {
    OC="$OC" python3 - <<'PY'
import json, os, sys

path = os.environ["OC"]
if not os.path.exists(path):
    print("opencode.json: not present, nothing to remove")
    sys.exit(0)
try:
    cfg = json.load(open(path))
except (ValueError, OSError) as e:
    sys.exit(f"install.sh: cannot parse {path}: {e}")

agents = cfg.get("agent")
if not isinstance(agents, dict) or "worker" not in agents:
    print("opencode.json: worker agent not set, nothing to remove")
    sys.exit(0)

del agents["worker"]
if not agents:
    cfg.pop("agent", None)

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("opencode.json: removed worker agent")
PY
}

py_set_statusline() {
    DIR="$DIR" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os, sys

path = os.environ["SETTINGS"]
d = os.environ["DIR"]
ours = {"type": "command", "command": d + "/statusline.sh", "refreshInterval": 1}

if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except (ValueError, OSError) as e:
        sys.exit(f"install.sh: cannot parse {path}: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"install.sh: {path} is not a JSON object")
else:
    cfg = {}

cur = cfg.get("statusLine")
if cur == ours:
    print("settings.json: statusLine already configured")
    sys.exit(0)
if cur is not None:
    print("settings.json: statusLine belongs to something else; left it untouched")
    print("  to use ds-bridge, set \"statusLine\" to: " + json.dumps(ours))
    sys.exit(0)

cfg["statusLine"] = ours
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("settings.json: statusLine configured")
PY
}

py_remove_statusline() {
    DIR="$DIR" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os, sys

path = os.environ["SETTINGS"]
d = os.environ["DIR"]
ours = {"type": "command", "command": d + "/statusline.sh", "refreshInterval": 1}

if not os.path.exists(path):
    print("settings.json: not present, nothing to remove")
    sys.exit(0)
try:
    cfg = json.load(open(path))
except (ValueError, OSError) as e:
    sys.exit(f"install.sh: cannot parse {path}: {e}")

if cfg.get("statusLine") != ours:
    print("settings.json: statusLine is not ours, left it untouched")
    sys.exit(0)

del cfg["statusLine"]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("settings.json: removed statusLine")
PY
}

py_set_guard() {
    DIR="$DIR" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os, sys

path = os.environ["SETTINGS"]
d = os.environ["DIR"]
cmd = d + "/hooks/guard.py"
ours = {"type": "command", "command": cmd}
entry = {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [ours]}

if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except (ValueError, OSError) as e:
        sys.exit(f"install.sh: cannot parse {path}: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"install.sh: {path} is not a JSON object")
else:
    cfg = {}

hooks = cfg.setdefault("hooks", {})
if not isinstance(hooks, dict):
    sys.exit(f"install.sh: {path} hooks is not a JSON object")
pre = hooks.setdefault("PreToolUse", [])
if not isinstance(pre, list):
    sys.exit(f"install.sh: {path} hooks.PreToolUse is not a list")

for e in pre:
    if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
        continue
    for h in e["hooks"]:
        if isinstance(h, dict) and h.get("command") == cmd:
            print("settings.json: guard hook already configured")
            sys.exit(0)

pre.append(entry)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("settings.json: guard hook configured")
PY
}

py_set_delegate() {
    DIR="$DIR" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os, sys

path = os.environ["SETTINGS"]
d = os.environ["DIR"]
cmd = d + "/hooks/delegate.py"
ours = {"type": "command", "command": cmd}
entry = {"hooks": [ours]}

if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except (ValueError, OSError) as e:
        sys.exit(f"install.sh: cannot parse {path}: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"install.sh: {path} is not a JSON object")
else:
    cfg = {}

hooks = cfg.setdefault("hooks", {})
if not isinstance(hooks, dict):
    sys.exit(f"install.sh: {path} hooks is not a JSON object")
# UserPromptSubmit: the per-turn reminder; SessionStart "compact": the playbook again after compaction
changed = False
for event, matcher in (("UserPromptSubmit", None), ("SessionStart", "compact")):
    lst = hooks.setdefault(event, [])
    if not isinstance(lst, list):
        sys.exit(f"install.sh: {path} hooks.{event} is not a list")
    if any(isinstance(e, dict) and isinstance(e.get("hooks"), list) and
           any(isinstance(h, dict) and h.get("command") == cmd for h in e["hooks"]) for e in lst):
        continue
    e = {"hooks": [dict(ours)]}
    if matcher:
        e["matcher"] = matcher
    lst.append(e)
    changed = True
if not changed:
    print("settings.json: delegate hooks already configured")
    sys.exit(0)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("settings.json: delegate hooks configured")
PY
}

py_remove_guard() {
    DIR="$DIR" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os, sys

path = os.environ["SETTINGS"]
cmd = os.environ["DIR"] + "/hooks/guard.py"

if not os.path.exists(path):
    print("settings.json: not present, nothing to remove")
    sys.exit(0)
try:
    cfg = json.load(open(path))
except (ValueError, OSError) as e:
    sys.exit(f"install.sh: cannot parse {path}: {e}")

hooks = cfg.get("hooks")
if not isinstance(hooks, dict):
    print("settings.json: guard hook not set, nothing to remove")
    sys.exit(0)
pre = hooks.get("PreToolUse")
if not isinstance(pre, list):
    print("settings.json: guard hook not set, nothing to remove")
    sys.exit(0)

keep, removed = [], 0
for e in pre:
    hit = False
    if isinstance(e, dict) and isinstance(e.get("hooks"), list):
        for h in e["hooks"]:
            if isinstance(h, dict) and h.get("command") == cmd:
                hit = True
                break
    if hit:
        removed += 1
    else:
        keep.append(e)

if not removed:
    print("settings.json: guard hook not set, nothing to remove")
    sys.exit(0)

if keep:
    hooks["PreToolUse"] = keep
else:
    del hooks["PreToolUse"]
if not hooks:
    del cfg["hooks"]

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("settings.json: removed the ds-bridge guard hook")
PY
}

py_remove_delegate() {
    DIR="$DIR" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os, sys

path = os.environ["SETTINGS"]
cmd = os.environ["DIR"] + "/hooks/delegate.py"

if not os.path.exists(path):
    print("settings.json: not present, nothing to remove")
    sys.exit(0)
try:
    cfg = json.load(open(path))
except (ValueError, OSError) as e:
    sys.exit(f"install.sh: cannot parse {path}: {e}")

hooks = cfg.get("hooks")
if not isinstance(hooks, dict):
    print("settings.json: delegate hook not set, nothing to remove")
    sys.exit(0)

removed = 0
for event in ("UserPromptSubmit", "SessionStart"):
    lst = hooks.get(event)
    if not isinstance(lst, list):
        continue
    keep = []
    for e in lst:
        hit = isinstance(e, dict) and isinstance(e.get("hooks"), list) and any(
            isinstance(h, dict) and h.get("command") == cmd for h in e["hooks"])
        if hit:
            removed += 1
        else:
            keep.append(e)
    if keep:
        hooks[event] = keep
    else:
        del hooks[event]

if not removed:
    print("settings.json: delegate hook not set, nothing to remove")
    sys.exit(0)
if not hooks:
    del cfg["hooks"]

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print("settings.json: removed the ds-bridge delegate hook")
PY
}

link_ds() {
    mkdir -p "$BIN_DIR"
    ln -sfn "$DIR/ds" "$LINK"
    say "linked $LINK -> $DIR/ds"
    case ":$PATH:" in
        *":$BIN_DIR:"*|*":$HOME/.local/bin:"*) ;;
        *) warn "$BIN_DIR is not on PATH; add it, e.g. export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
}

unlink_ds() {
    if [ -L "$LINK" ] && [ "$(readlink -f "$LINK")" = "$DIR/ds" ]; then
        rm -f "$LINK"
        say "removed $LINK"
    elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
        say "left $LINK alone (not a symlink to $DIR/ds)"
    else
        say "$LINK not present, nothing to remove"
    fi
}

restart_opencode() {
    if opencode service restart >/dev/null 2>&1; then
        say "restarted the OpenCode service"
    fi
}

link_skill() {
    local dest="$CLAUDE_DIR/skills/delegate"
    mkdir -p "$CLAUDE_DIR/skills"
    if [ -L "$dest" ] || [ ! -e "$dest" ]; then
        ln -sfn "$DIR/skills/delegate" "$dest"
        say "linked /delegate skill -> $dest"
    else
        warn "$dest exists and is not a link; leaving it (remove it to use the repo's skill)"
    fi
}

unlink_skill() {
    local dest="$CLAUDE_DIR/skills/delegate"
    if [ -L "$dest" ]; then
        rm "$dest"
        say "removed /delegate skill link"
    fi
}

do_uninstall() {
    command -v python3 >/dev/null 2>&1 || {
        warn "python3 is required for --uninstall"
        exit 1
    }
    py_remove_agent
    unlink_ds
    py_remove_statusline
    py_remove_guard
    py_remove_delegate
    unlink_skill
}

do_install() {
    require_commands

    mkdir -p "$OC_DIR"
    if [ -f "$OC" ] && [ ! -f "$OC_BAK" ]; then
        cp "$OC" "$OC_BAK"
        say "backed up $OC -> $OC_BAK"
    fi
    py_merge_agent
    say "merged worker agent into $OC"

    link_ds

    mkdir -p "$CLAUDE_DIR"
    py_set_statusline
    py_set_guard
    py_set_delegate
    link_skill
    mkdir -p "$HOME/.config/ds-bridge"
    touch "$HOME/.config/ds-bridge/guard-off"
    say "delegation is opt-in per chat: /delegate (or 'ds delegate on') turns it on;"
    say "  the guard then blocks direct edits in that chat only."
    say "  to guard every chat instead: rm ~/.config/ds-bridge/guard-off"

    restart_opencode
    say ""
    say "Add the rules in claude-rules.md to ~/.claude/CLAUDE.md (or a project CLAUDE.md) so Claude knows the delegation rules."
}

case "${1:-}" in
    --uninstall) do_uninstall ;;
    "") do_install ;;
    *)
        warn "unknown argument: $1"
        say "usage: ./install.sh [--uninstall]"
        exit 2
        ;;
esac
