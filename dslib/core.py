"""ds-bridge: config, models, brief lint and small helpers shared by everything."""
import hashlib, json, os, signal, subprocess, sys, time, argparse, shutil, queue, threading, fcntl
import fnmatch, re, tempfile, base64, collections, urllib.request, urllib.parse, secrets
import contextlib, io

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))   # the ds-bridge folder
CONF = os.path.join(HOME, ".config/ds-bridge/config.json")
DELEGATE = os.path.join(HOME, ".local/state/ds-bridge/delegate")
DEFAULTS = {"model": "opencode-go/deepseek-v4.1-flash", "agent": "worker",
            "escalation_model": "", "worker_rules": "agent", "takeover": "ask",
            "models": {"flash": "opencode-go/deepseek-v4.1-flash",
                       "muse": "opencode-go/muse-spark-1.3-contributor"},
            "max_diff_lines": 250, "max_report_chars": 1500, "timeout_s": 1800,
            "soft_timeout_frac": 0.8, "wrapup_s": 180,
            "max_files": 20000, "big_file_mb": 5,
            "fix_tries": 2, "check_timeout_s": 600, "job_budget_s": 1800,
            "overload_retries": 2, "overload_wait_s": [30, 90],
            "excluded_note": True, "diff_on_pass": "stat", "research_words": 200,
            "research_agent": "researcher", "guard_lines": 20,
            "fallback_model": "", "guard_edits_per_file": 2,
            "auto_land": True, "max_parallel": 6, "wait_s": 540, "until_done_max_s": 14400, "max_block_chars": 1600,
            "notify": True, "notify_min_s": 15,
            "outside_ignore": [".git", "__pycache__", "media*", "*.log",
                               "node_modules", ".venv"]}


_CONFIG_CACHE = None


def cfg():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    c = dict(DEFAULTS)
    try:
        with open(CONF) as f:
            data = json.load(f)
    except FileNotFoundError:
        _CONFIG_CACHE = env_overrides(c)
        return c
    except ValueError as e:
        die(f"config.json invalid: {e}")
    except OSError as e:
        die(f"config.json invalid: {e}")
    if not isinstance(data, dict):
        die("config.json invalid: top-level value must be an object")
    c.update(data)
    _CONFIG_CACHE = env_overrides(c)
    return c


def env_overrides(c):
    """DS_AGENT / DS_WORKER_RULES / DS_WAIT_S override the config for one invocation (worker A/B tests)."""
    for env, key in (("DS_AGENT", "agent"), ("DS_WORKER_RULES", "worker_rules")):
        if os.environ.get(env):
            c[key] = os.environ[env]
    if os.environ.get("DS_WAIT_S", "").isdigit():   # tests: detach sooner
        c["wait_s"] = int(os.environ["DS_WAIT_S"])
    return c


def die(msg, code=2):
    print(f"ds: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- brief lint
PLAYBOOK = os.path.join(ROOT, "planner-playbook.md")
BRIEF_SECTIONS = ["GOAL", "CONTEXT", "FILES", "STEPS", "DO NOT", "DONE WHEN"]
BRIEF_REQUIRED = ["GOAL", "FILES", "STEPS", "DO NOT", "DONE WHEN"]
# phrases that leave a decision to the worker; each is refused outside backticks and DO NOT
VAGUE_WORDS = [r"\betc\b", r"\band so on\b", r"\bas needed\b", r"\bif needed\b",
               r"\bif necessary\b", r"\bappropriate(ly)?\b", r"\bsomething like\b",
               r"\band the like\b", r"\bwhatever\b", r"\bclean(\s|-)?up\b", r"\bimprove\b",
               r"\bhandle (it )?properly\b", r"\bmake sure it works\b", r"\bsimilar to\b",
               r"\bor so\b", r"\bsomehow\b", r"\bfigure out\b", r"\bas you see fit\b",
               r"\bmaybe\b", r"\bperhaps\b", r"\bsome kind of\b", r"\bnice\b"]
# "GOAL: text", "GOAL:" or a bare "GOAL" line (optionally "## GOAL" / "**GOAL**") all count
_HEAD = re.compile(r"^[ \t]*(?:#+[ \t]*)?\**[ \t]*(" + "|".join(BRIEF_SECTIONS)
                   + r")[ \t]*\**[ \t]*(?::|$)", re.M)


def resolve_model(name):
    """-m accepts a short name from config "models" (flash, muse) or a full provider/model id."""
    if not name:
        return None
    return (cfg().get("models") or {}).get(name.strip(), name.strip())


def other_model(model):
    """The model a fallback or escalation should use when it would be the job's own model:
    the default model (so a Muse job falls back to Flash and the other way round)."""
    c = cfg()
    return c["model"] if c["model"] != model else None


def worker_rules_text():
    with open(os.path.join(os.path.dirname(PLAYBOOK), "worker-prompt.md")) as f:
        return f.read().strip()


def playbook_text():
    try:
        with open(PLAYBOOK) as f:
            return f.read()
    except OSError:
        return "(planner-playbook.md not found next to ds)"


def brief_template():
    t = playbook_text()
    m = re.search(r"## Writing the brief.*?```\n(.*?)```", t, re.S)
    return m.group(1).rstrip() if m else t


def brief_sections(task):
    """{section: text} for the sectioned brief."""
    out, heads = {}, list(_HEAD.finditer(task))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(task)
        out[h.group(1)] = task[h.end():end].strip()
    return out


def _vague(text):
    plain = re.sub(r"`[^`]*`", " ", text)          # exact strings may say anything
    found = []
    for pat in VAGUE_WORDS:
        for m in re.finditer(pat, plain, re.I):
            found.append(m.group(0))
    return sorted(set(w.lower() for w in found))


def lint_brief(task):
    """Problems with a run brief, [] if it may go to the worker."""
    c = cfg()
    probs = []
    if len(task.strip()) < c.get("brief_min_chars", 300):
        probs.append(f"too short ({len(task.strip())} chars, need {c.get('brief_min_chars', 300)}): "
                     "a self-contained brief names files, places, exact changes and checks")
    sec = brief_sections(task)
    for name in BRIEF_REQUIRED:
        if name not in sec:
            probs.append(f"missing section {name}:")
        elif not sec[name]:
            probs.append(f"section {name}: is empty")
    steps = sec.get("STEPS", "")
    if "STEPS" in sec and not re.search(r"^[ \t]*\d+[.)][ \t]", steps, re.M):
        probs.append("STEPS: needs numbered steps (1. 2. 3.), one action each")
    rest = "\n".join(v for k, v in sec.items() if k != "DO NOT") if sec else task
    words = _vague(rest)
    if words:
        probs.append("vague words (decide it and write it out): " + ", ".join(repr(w) for w in words))
    return probs


def lint_feedback(text):
    probs = []
    if not re.search(r"^[ \t]*\d+[.)][ \t]", text, re.M):
        probs.append("feedback needs numbered points (1. 2. ...), each naming the step, "
                     "the file and place, and the exact correction")
    words = _vague(text)
    if words:
        probs.append("vague words (write the exact correction): " + ", ".join(repr(w) for w in words))
    return probs


def refuse_brief(kind, probs):
    msg = [f"brief refused ({kind}); nothing was run.", *("  - " + p for p in probs)]
    if kind == "run":
        msg += ["", "Template:", brief_template()]
    msg += ["", "Full rules: ds playbook"]
    print("\n".join(msg), file=sys.stderr)
    sys.exit(2)


def install_signal_handlers():
    """Turn SIGTERM/SIGHUP into SystemExit so run/revise finally blocks run."""
    def _exit(signum, frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _exit)
        except (ValueError, OSError):
            pass


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _median(vals):
    vals = sorted(vals)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def fmt_num(n):
    """Format a token count as 1.2M / 340k / 912."""
    n = int(round(n))
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1_000_000:
        return f"{sign}{a / 1_000_000:.1f}M" if a < 10_000_000 else f"{sign}{a / 1_000_000:.0f}M"
    if a >= 1_000:
        return f"{sign}{a / 1_000:.1f}k" if a < 10_000 else f"{sign}{a / 1_000:.0f}k"
    return f"{sign}{a}"


def _clip(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def fmt_secs(n):
    n = int(n)
    return f"{n // 3600}:{n % 3600 // 60:02d}:{n % 60:02d}" if n >= 3600 else f"{n // 60}:{n % 60:02d}"


def read_marker(marker):
    try:
        raw = open(marker).read().strip()
    except OSError:
        return None
    if not raw:
        return {}
    try:
        st = json.loads(raw)
        return st if isinstance(st, dict) else {"shown": True}
    except ValueError:
        return {"shown": True}


def chat_model():
    """The worker model chosen for this chat (`ds delegate on --model` / `ds delegate model`), or None."""
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        return None
    st = read_marker(os.path.join(DELEGATE, sid))
    return resolve_model(st.get("model")) if st else None


def short_model(m):
    names = {v: k for k, v in (cfg().get("models") or {}).items()}
    return names.get(m, m.split("/")[-1])


__all__ = [n for n in globals() if not n.startswith("__")]
