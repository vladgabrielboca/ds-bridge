"""ds-bridge: where a job works: shadow snapshots, git worktree copies, paths, running markers."""
import hashlib, json, os, signal, subprocess, sys, time, argparse, shutil, queue, threading, fcntl
import fnmatch, re, tempfile, base64, collections, urllib.request, urllib.parse, secrets
import contextlib, io
from .core import *  # noqa: F401,F403
STATE = os.path.join(HOME, ".local/share/ds-bridge")
JOBS = os.path.join(STATE, "jobs")
SHADOW = os.path.join(STATE, "shadow")
RUNNING = os.path.join(STATE, "running")
WORK = os.path.join(STATE, "work")
# Never walk into these: dependency/cache dirs can be huge and their changes
# are already reported by size, not by file.
EXCLUDED_PRUNE = {"node_modules", ".venv", "venv", "__pycache__", ".git", ".cache"}
EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".git/", "*.pyc",
            "media/", "*.mp4", "*.mkv", "*.mov", "*.wav", "*.mp3", "*.flac",
            "*.safetensors", "*.bin", "*.pt", "*.pth", "*.ckpt", "*.gguf", "*.onnx",
            "*.zip", "*.tar*", "*.iso", ".cache/", "dist/", "build/", ".next/"]


def git(job_or_gd, wt, *args, check=True, capture=True, index=None):
    gd = job_or_gd
    env = os.environ.copy()
    if index:
        env["GIT_INDEX_FILE"] = index
    # a landed copy job's folder is gone; commit-to-commit diffs only need the shadow repo
    r = subprocess.run(["git", "-c", "core.quotePath=false",
                        f"--git-dir={gd}", f"--work-tree={wt}", *args],
                       capture_output=capture, text=True, cwd=wt if os.path.isdir(wt) else gd, env=env)
    if check and r.returncode:
        die(f"git {' '.join(args)} failed: {r.stderr.strip()[:500]}")
    return r.stdout if capture else ""


def job_index(jid):
    return os.path.join(JOBS, jid, "index")


def shadow_for(d):
    gd = os.path.join(SHADOW, hashlib.sha1(d.encode()).hexdigest()[:16])
    os.makedirs(SHADOW, exist_ok=True)
    # Serialize first-time setup so two concurrent jobs cannot race git init.
    with open(gd + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not os.path.isdir(gd):
            os.makedirs(gd)
            subprocess.run(["git", "init", "-q", "--bare", gd], check=True)
            for k, v in [("core.bare", "false"), ("user.name", "ds-bridge"),
                         ("user.email", "ds-bridge@localhost"), ("core.autocrlf", "false"),
                         ("gc.auto", "0")]:
                subprocess.run(["git", f"--git-dir={gd}", "config", k, v], check=True)
        os.makedirs(os.path.join(gd, "info"), exist_ok=True)
    return gd


def write_exclude(gd, ex):
    p = os.path.join(gd, "info/exclude")
    tmp = p + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write("\n".join(ex) + "\n")
    os.replace(tmp, p)


def stage_paths(gd, wt, paths, index):
    """Stage paths; missing paths that never existed are simply skipped."""
    if not paths:
        git(gd, wt, "add", "-A", index=index)
        return
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = index
    for p in paths:
        r = subprocess.run(["git", "-c", "core.quotePath=false",
                            f"--git-dir={gd}", f"--work-tree={wt}",
                            "add", "-A", "--", p],
                           capture_output=True, text=True, cwd=wt, env=env)
        if r.returncode and "did not match any files" not in (r.stderr or ""):
            die(f"git add {p} failed: {(r.stderr or '').strip()[:500]}")


def snapshot(d, gd, label, index, jid, paths=None):
    c = cfg()
    ex = list(EXCLUDES)
    # skip big files so snapshots stay fast/small
    env = dict(os.environ, PWD=d)
    r = subprocess.run(["find", ".", "-path", "./.git", "-prune", "-o", "-type", "f",
                        "-size", f"+{c['big_file_mb']}M", "-print"],
                       capture_output=True, text=True, cwd=d, env=env)
    ex += ["/" + p[2:] for p in r.stdout.splitlines() if p.startswith("./")]
    write_exclude(gd, ex)
    stage_paths(gd, d, paths, index)
    n = len([f for f in git(gd, d, "ls-files", "-z", index=index).split("\0") if f])
    if n > c["max_files"]:
        die(f"{d} has {n} tracked files (> {c['max_files']}); run in a smaller directory")
    tree = git(gd, d, "write-tree", index=index).strip()
    commit = git(gd, d, "commit-tree", tree, "-m", label, index=index).strip()
    git(gd, d, "update-ref", f"refs/ds/{jid}/{label.replace(' ', '')}", commit, index=index)
    return commit


def norm_paths(paths):
    if not paths:
        return None
    out = []
    for p in paths:
        q = os.path.normpath(p).replace(os.sep, "/")
        if os.path.isabs(q) or q == "." or q == ".." or q.startswith("../"):
            die(f"--paths must be relative to the -C directory: {p}")
        if q not in out:
            out.append(q)
    return out


def outside_ignored(rel, ignores):
    parts = rel.split("/")
    for pat in ignores:
        if fnmatch.fnmatch(rel, pat):
            return True
        if any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def under_paths(rel, paths):
    for p in paths:
        p = p.rstrip("/")
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


def outside_listing(d, paths):
    """Cheap (size, mtime) listing of files outside the job's --paths."""
    ignores = cfg().get("outside_ignore") or []
    out = {}
    for root, dirs, names in os.walk(d):
        rel_root = os.path.relpath(root, d)
        dirs[:] = [x for x in dirs
                   if not outside_ignored((x if rel_root == "." else rel_root + "/" + x),
                                          ignores)]
        for name in names:
            rel = name if rel_root == "." else rel_root + "/" + name
            rel = rel.replace(os.sep, "/")
            if under_paths(rel, paths) or outside_ignored(rel, ignores):
                continue
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def git_ignored(d, rels):
    """The subset of rels that d's git ignores (generated files: a check's build output, renders).
    They never land through the patch, so they must not count as changes outside --paths."""
    if not rels:
        return set()
    try:
        r = subprocess.run(["git", "-C", d, "check-ignore", "-z", "--stdin"], input="\0".join(rels) + "\0",
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(x for x in r.stdout.split("\0") if x) if r.returncode in (0, 1) else set()


def outside_changes(before, after, d=None):
    """Return capped lines for files created, changed or deleted outside --paths (git-ignored
    files in d excluded)."""
    found = [(rel, "created") for rel in after if rel not in before]
    found += [(rel, "changed") for rel in after if rel in before and before[rel] != after[rel]]
    found += [(rel, "deleted") for rel in before if rel not in after]
    ignored = git_ignored(d, [rel for rel, _ in found]) if d else set()
    lines = [f"{what} {rel}" for rel, what in sorted(found, key=lambda x: (x[1] == "deleted", x[0]))
             if rel not in ignored]
    if len(lines) > 20:
        lines = lines[:19] + [f"… {len(lines) - 19} more"]
    return lines


def excluded_listing(d, paths):
    """Cheap (size, mtime_ns) listing for files that snapshots may skip.

    Dependency/cache directories are pruned, never entered. Files are listed
    here (not just pattern matches) so that a file that grew past
    big_file_mb is reported as changed rather than created; the reporter
    below keeps only excluded files.
    """
    out = {}
    for root, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in EXCLUDED_PRUNE]
        rel_root = os.path.relpath(root, d)
        for name in names:
            rel = name if rel_root == "." else rel_root + "/" + name
            rel = rel.replace(os.sep, "/")
            if paths and not under_paths(rel, paths):
                continue
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def excluded_name(rel):
    """True when rel matches an EXCLUDES pattern."""
    parts = rel.split("/")
    name = parts[-1]
    for pat in EXCLUDES:
        if pat.endswith("/"):
            if pat[:-1] in parts:
                return True
        elif fnmatch.fnmatch(name, pat):
            return True
    return False


def excluded_note(before, after, c=None):
    """One tiny line summarising changes snapshots cannot see, or None."""
    c = c or cfg()
    if not c.get("excluded_note", True):
        return None
    try:
        big = float(c.get("big_file_mb", 5)) * 1024 * 1024
    except (TypeError, ValueError):
        big = 5 * 1024 * 1024
    groups = collections.defaultdict(lambda: [0, 0, 0])  # changed, created, deleted
    for rel in set(before) | set(after):
        b, a = before.get(rel), after.get(rel)
        if not excluded_name(rel) and not (b and b[0] > big) and not (a and a[0] > big):
            continue
        if b is None:
            kind = 1
        elif a is None:
            kind = 2
        elif b != a:
            kind = 0
        else:
            continue
        parts = rel.split("/")
        key = parts[0] + "/" if len(parts) > 1 else rel
        groups[key][kind] += 1
    if not groups:
        return None
    items = sorted(groups.items(), key=lambda kv: (-sum(kv[1]), kv[0]))
    bits = []
    for key, counts in items[:5]:
        seg = [key]
        for label, i in (("changed", 0), ("created", 1), ("deleted", 2)):
            if counts[i]:
                seg.append(f"{counts[i]} {label}")
        bits.append(" ".join(seg))
    line = "--- NOTE not in diff, not undoable by reject: " + " · ".join(bits)
    if len(items) > 5:
        line += f" · +{len(items) - 5} more groups"
    return line


def paths_overlap(a, b):
    if a is None or b is None:
        return True
    for x in a:
        for y in b:
            x, y = x.rstrip("/"), y.rstrip("/")
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def check_overlap(d, paths):
    os.makedirs(RUNNING, exist_ok=True)
    for name in sorted(os.listdir(RUNNING)):
        if not name.endswith(".json"):
            continue
        p = os.path.join(RUNNING, name)
        try:
            m = json.load(open(p))
        except (OSError, ValueError):
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        if not pid_alive(m.get("pid")):
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        if m.get("isolated"):
            continue   # a job in its own worktree never edits the project until it lands
        if os.path.realpath(m.get("dir", "")) == d and paths_overlap(m.get("paths"), paths):
            other = name[:-5]
            die(f"job {other} is already running in {d}; wait for it or use disjoint --paths")


def write_marker(jid, d, paths, isolated=False):
    os.makedirs(RUNNING, exist_ok=True)
    p = os.path.join(RUNNING, jid + ".json")
    tmp = p + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump({"dir": d, "paths": paths, "pid": os.getpid(), "isolated": isolated}, f)
    os.replace(tmp, p)


def clear_marker(jid):
    try:
        os.remove(os.path.join(RUNNING, jid + ".json"))
    except OSError:
        pass


def proj(job):
    """The user's project dir; job["dir"] is the private copy when the job has one."""
    return job.get("project") or job["dir"]


def want_copy(c, paths, force=False):
    # cheap-agent's model: every job runs in its own git worktree (config "isolate": "never" = in place)
    mode = c.get("isolate", "always")
    if force or mode == "always":
        return True
    if mode == "never":
        return False
    return bool(paths)


# ---- git: every project is a repo (ds inits it, no commit needed); jobs run in worktrees ----

GITIGNORE_DEFAULT = ["# written by ds-bridge (git init); edit freely",
                     "__pycache__/", "*.pyc", ".venv/", "venv/", "node_modules/",
                     "media*/", "*.mp4", "*.mkv", "*.mov", "*.wav", "*.mp3", "*.flac",
                     "*.safetensors", "*.pt", "*.pth", "*.ckpt", "*.gguf", "*.onnx", "*.bin",
                     ".cheapag/", ".DS_Store"]
# ignored folders a job must see but never rebuild are linked into its worktree (cheap-agent's
# link_dirs, generalised to every ignored folder); build output and caches are not
LINK_SKIP = {"__pycache__", "dist", "build", "out", "target", "coverage", ".next", ".nuxt",
             ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cheapag", ".git",
             "media_check"}   # check scratch: parallel jobs would read each other's findings


def link_skip():
    return LINK_SKIP | set(x for x in (cfg().get("link_skip") or []) if isinstance(x, str))
GIT_ENV = {"GIT_AUTHOR_NAME": "ds-bridge", "GIT_AUTHOR_EMAIL": "ds-bridge@localhost",
           "GIT_COMMITTER_NAME": "ds-bridge", "GIT_COMMITTER_EMAIL": "ds-bridge@localhost"}


def g(repo, *args, check=True, env=None, input=None):
    r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", repo, *args], capture_output=True,
                       text=True, env=dict(os.environ, **(env or {})), input=input)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def repo_root(d):
    r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def containers():
    c = cfg()
    out = {os.path.realpath(os.path.expanduser(x)) for x in
           ["~", "~/Work", "~/Projects", "~/Projects/videos", "/", "/tmp"]}
    out |= {os.path.realpath(os.path.expanduser(x)) for x in (c.get("no_init") or [])}
    return out


def ensure_repo(d):
    """The git repo holding d; git-inits d first when there is none (refusing folders of projects
    and folders too big to be one project). Returns (root, note or None)."""
    root = repo_root(d)
    if root:
        return root, None
    real = os.path.realpath(d)
    if real in containers():
        die(f"{d} is a folder of projects, not a project: pass the project folder with -C "
            "(ds git-inits it if needed)")
    try:
        kids = sorted(x for x in os.listdir(real) if os.path.isdir(os.path.join(real, x, ".git")))
    except OSError as e:
        die(f"cannot read {d}: {e}")
    if kids:
        die(f"{d} holds other git repos ({', '.join(kids[:3])}): pass one project folder with -C")
    c = cfg()
    os.makedirs(STATE, exist_ok=True)
    lock_path = os.path.join(STATE, "init-" + hashlib.sha1(real.encode()).hexdigest()[:12] + ".lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        root = repo_root(d)
        if root:
            return root, None
        gi = os.path.join(real, ".gitignore")
        made_gi = not os.path.exists(gi)
        g(real, "init", "-q")
        if made_gi:
            with open(gi, "w") as f:
                f.write("\n".join(GITIGNORE_DEFAULT) + "\n")
        files = [x for x in g(real, "ls-files", "-o", "--exclude-standard", "-z").split("\0") if x]
        size = 0
        for x in files:
            try:
                size += os.lstat(os.path.join(real, x)).st_size
            except OSError:
                pass
        max_n, max_mb = int(c.get("init_max_files", 20000)), int(c.get("init_max_mb", 200))
        if len(files) > max_n or size > max_mb * 1024 * 1024:
            shutil.rmtree(os.path.join(real, ".git"), ignore_errors=True)
            if made_gi:
                os.remove(gi)
            die(f"{d}: {len(files)} files / {size // (1024 * 1024)} MB that git would track (limit "
                f"{max_n} / {max_mb} MB), so it does not look like one project. Pass the project "
                "folder with -C, or add ignores to its .gitignore first.")
    note = (f"git: initialized {real} (no commit made"
            + ("; wrote .gitignore for media, renders, audio, .venv, caches" if made_gi else "") + ")")
    return real, note


def make_copy(d, name):
    """A git worktree of d's repo under WORK holding the project's current state (committed or not,
    without needing a commit), ignored folders linked in. Returns (job dir, None) or (None, reason)."""
    root = repo_root(d)
    if not root:
        return reflink_copy(d, name)
    rel = os.path.relpath(os.path.realpath(d), root)
    os.makedirs(WORK, exist_ok=True)
    dst = os.path.join(WORK, name)
    remove_worktree(dst, root)
    idx = os.path.join(WORK, f".{name}.index")
    env = dict(GIT_ENV, GIT_INDEX_FILE=idx)
    try:
        head = g(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
        if head:
            g(root, "read-tree", head, env=env)
        else:
            g(root, "read-tree", "--empty", env=env)
        # the current files: tracked ones as they are now, plus untracked ones git does not ignore
        g(root, "add", "-A", env=env)
        tree = g(root, "write-tree", env=env).strip()
        commit = g(root, "commit-tree", tree, *(["-p", head] if head else []), "-m", f"ds base {name}",
                   env=env).strip()
        g(root, "worktree", "add", "-q", "--detach", "-f", dst, commit)
    except RuntimeError as e:
        remove_worktree(dst, root)
        return None, str(e)
    finally:
        try:
            os.remove(idx)
        except OSError:
            pass
    link_ignored(root, dst)
    return (dst if rel == "." else os.path.join(dst, rel)), None


def link_ignored(root, dst):
    """Ignored folders (media, renders, .venv, node_modules…) become links to the project's own;
    ignored loose files are reflinked. cheap-agent ADR-0003: what a job writes into a linked folder
    lands in the project directly (renders), code changes still land only on a pass."""
    out = g(root, "ls-files", "-o", "-i", "--exclude-standard", "--directory", "-z", check=False)
    for e in [x for x in out.split("\0") if x]:
        src = os.path.join(root, e.rstrip("/"))
        tgt = os.path.join(dst, e.rstrip("/"))
        if os.path.lexists(tgt) or any(part in link_skip() for part in e.rstrip("/").split("/")):
            continue
        try:
            os.makedirs(os.path.dirname(tgt), exist_ok=True)
            if e.endswith("/") or os.path.isdir(src):
                os.symlink(src, tgt)
            else:
                subprocess.run(["cp", "-a", "--reflink=auto", src, tgt], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            pass


def worktree_top(path):
    """The worktree folder under WORK that holds path, or None."""
    real, work = os.path.realpath(path or ""), os.path.realpath(WORK)
    if not real.startswith(work + os.sep):
        return None
    return os.path.join(work, os.path.relpath(real, work).split(os.sep)[0])


def remove_worktree(top, repo=None):
    if not top or not os.path.lexists(top):
        return
    # rmtree never follows symlinks, so the linked media/.venv folders stay untouched
    shutil.rmtree(top, ignore_errors=True)
    if repo:
        g(repo, "worktree", "prune", check=False)


def reflink_copy(d, name):
    """Copy-on-write copy of d under WORK; (path, None) or (None, reason)."""
    os.makedirs(WORK, exist_ok=True)
    dst = os.path.join(WORK, name)
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    r = subprocess.run(["cp", "-a", "--reflink=always", d + "/.", dst],
                       capture_output=True, text=True)
    if r.returncode:
        shutil.rmtree(dst, ignore_errors=True)
        why = (r.stderr or "").strip().splitlines()
        return None, (why[-1][:160] if why else f"cp exit {r.returncode}")
    return dst, None


def drop_copy(job):
    w = job.get("dir")
    if job.get("project") and w:
        remove_worktree(worktree_top(w), job.get("repo") or repo_root(job["project"]))


_UP = re.compile(r"(?<![\w./~-])((?:\.\./)+[\w.@%+=:,/-]*)")


def abs_outside(text, d):
    """`../x` paths that leave the project d and exist become absolute: the job runs in a copy
    somewhere else, where a relative path out of the project points at nothing."""
    if not text or "../" not in text:
        return text

    def fix(m):
        full = os.path.normpath(os.path.join(d, m.group(1)))
        inside = full == d or full.startswith(d.rstrip("/") + "/")
        return full if not inside and os.path.exists(full) else m.group(1)
    return _UP.sub(fix, text)


def to_copy(text, d, w):
    """Point absolute paths into the project at the private copy instead."""
    if not text or not w or w == d:
        return text
    return re.sub(re.escape(d) + r"(?=$|[/\s\"'`:;,)\]])", w, text)


__all__ = [n for n in globals() if not n.startswith("__")]
