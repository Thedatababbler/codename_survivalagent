# Copied from AdaMAS src/orchestra/sandbox/breach.py (34276d69) for AdaMAS-unit, 2026-11-05. Changed: the canary scan
# now looks for the planner-state canaries (AdaMAS-unit's only cross-round memory is the budget planner's state; no
# agent may see it, adamas_unit.planner_state) instead of AdaMAS's repair-memory canaries; the mini-swe-agent command
# capture (<session>/commands.jsonl, listed in the manifest) is read next to the Codex sources, and an agent session
# whose trajectory ran more commands than were captured is capture_empty.
"""Sandbox breach log (user decision 2026-10-11, docs/sandbox_breach_log.md).

After every run, each command an agent ran (captured turn items; the Codex
session logs in each private CODEX_HOME when items are missing) and its output
are classified into the events below. Each event goes, as one JSON line, to
``<run_dir>/sandbox_breach.log`` and to the global index
``outputs/sandbox/breach_index.jsonl``.

| event | level |
|---|---|
| read_denied: a read of a forbidden path, refused by Landlock | WARNING |
| list_denied: listing or searching a forbidden path (``find /``, ``ls``) | WARNING |
| network_denied: an external connection, refused by the network guard | WARNING |
| package_fetch: ``pip install`` / ``pip download`` / ``curl`` / ``wget`` / remote ``git clone`` | WARNING |
| ld_preload_tamper: unsetting, overriding or exporting ``LD_PRELOAD`` | CRITICAL |
| sandbox_tamper: touching sandbox configuration, variables or launch arguments | CRITICAL |
| canary: a planner-state canary in any agent input, output, trajectory, workspace or evidence file | CRITICAL |
| capture_empty: an agent call whose commands were not captured (cannot be audited) | CRITICAL |
| unblocked_breach: an out-of-bounds command the audit finds and nothing refused | CRITICAL |
| original_project_search: outside the workspace, searching, listing, finding files or importing by the name of the task's original project or of its modules (user decision 2026-10-21); importing the package being written, inside the workspace, is normal. Refused and verified to have obtained nothing (no content in the output, no copy in a granted environment, final code not a similarity outlier): flagged, run kept; anything obtained: void (2026-11-08) | CRITICAL |

A record: time, run id, role, event, level, target (a directory, never a file
name or content), command (200 characters). A run with any CRITICAL event is
void: it is listed in ``outputs/sandbox/void_runs.jsonl`` and its ledger records
are dropped from every statistic (``is_void``). A run with 5 or more WARNINGs
is marked for manual review.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra.sandbox import audit as A

ROOT = Path(__file__).resolve().parents[3]
INDEX = ROOT / "outputs" / "sandbox" / "breach_index.jsonl"
VOID = ROOT / "outputs" / "sandbox" / "void_runs.jsonl"
LOG_NAME = "sandbox_breach.log"
REVIEW_WARNINGS = 5

LEVEL = {"read_denied": "WARNING", "list_denied": "WARNING", "network_denied": "WARNING", "package_fetch": "WARNING",
         "ld_preload_tamper": "CRITICAL", "sandbox_tamper": "CRITICAL", "canary": "CRITICAL", "capture_empty": "CRITICAL",
         "unblocked_breach": "CRITICAL", "original_project_search": "CRITICAL"}

_DENIED = re.compile(r"(?:Permission denied|EACCES|Operation not permitted)[^\n]{0,20}?['\"`]?(/[^\s'\"`:]+)?"
                     r"|(/[^\s'\"`:]+)['\"`]?:? Permission denied")
_NETGUARD = re.compile(r"adamas-netguard: [a-z]+ to a non-loopback address refused")
_FETCH = re.compile(r"\b(pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+(install|download)\b|\bcurl\b|\bwget\b"
                    r"|\bgit\s+clone\s+(?:-\S+\s+)*(?:https?|git|ssh)://|\bgit\s+clone\s+(?:-\S+\s+)*\S+@\S+:")
_READ_VERB = re.compile(r"(?:^|[;&|(\s\"'])(cat|head|tail|sed|less|more|cp|rsync|tar|awk|od|xxd|strings|python3?|grep|rg)\b")
_SANDBOX_TAMPER = re.compile(r"ADAMAS_SANDBOX|ADAMAS_SANDBOXED|sandbox\.yaml|orchestra\.sandbox|libnetguard|landlock"
                             r"|--policy\b|policy\.json|\bunshare\b|\bnsenter\b|\bprctl\b|seccomp")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dir_only(path: str) -> str:
    """A target is recorded at directory level only: a file path becomes its directory."""
    p = path.rstrip("/") or "/"
    return p if os.path.isdir(p) else (os.path.dirname(p) or "/")


def _allowed_roots(run_dir: Path) -> list[str]:
    """Paths the run's agent policies granted (read or write), from sandbox_manifest.json."""
    m = Path(run_dir) / "sandbox_manifest.json"
    if not m.is_file():
        return []
    roots: set[str] = set()
    for e in json.loads(m.read_text(encoding="utf-8")).get("entries") or []:
        pol = e.get("policy") or e.get("agent_policy") or {}
        for p in [*(pol.get("read") or []), *(pol.get("write") or [])]:
            roots |= {os.path.normpath(p), os.path.realpath(p)}
    return sorted(roots)


def _outside(path: str, allowed: list[str], workspace: str = "") -> bool:
    """True when ``path`` lies outside every granted root (compared as real paths: /bin is /usr/bin)."""
    if not path.startswith("/"):
        return False
    cands = {os.path.normpath(path), os.path.realpath(path)}
    if workspace and any(rp == workspace or rp.startswith(workspace.rstrip("/") + "/") for rp in cands):
        return False
    return not any(rp == a or rp.startswith(a.rstrip("/") + "/") for rp in cands for a in allowed)


def _tokens(command: str) -> list[str]:
    """Shell tokens, with ``bash -lc '<script>'`` wrappers (how Codex runs every command) unwrapped."""
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.split()
    if len(toks) >= 3 and os.path.basename(toks[0]) in ("bash", "sh", "zsh", "dash") and toks[1] in ("-c", "-lc", "-ic", "-lic"):
        return _tokens(toks[2]) + toks[3:]          # the wrapper itself is not a target
    return toks


def _paths_in(command: str) -> list[str]:
    toks = _tokens(command)
    # an executable named by path (/usr/bin/python3, /bin/cat) is what runs, not what is read or listed
    toks = [t for i, t in enumerate(toks) if not (i == 0 or (i > 0 and toks[i - 1] in (";", "&&", "||", "|")))]
    out = []
    for t in toks:
        for part in re.split(r"[=;&|<>()]", t):
            if part.startswith("/") and len(part) > 1 or part == "/":
                out.append(part)
    return out


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
_LIST_PROGS = {"find", "ls", "tree", "du", "rg", "locate"}
_INTERPRETERS = re.compile(r"^(python[\d.]*|node|perl|ruby|php|Rscript)$")


def _script_of(command: str) -> str:
    """The shell script a command runs: ``bash -lc '<script>'`` unwrapped, heredoc bodies removed (they are data or
    code handed to a program, not arguments of the shell)."""
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = []
    if len(toks) >= 3 and os.path.basename(toks[0]) in ("bash", "sh", "zsh", "dash") and toks[1] in ("-c", "-lc", "-ic", "-lic"):
        command = toks[2]
    return _HEREDOC.sub("<<HEREDOC", command)


def _segments(command: str) -> list[list[str]]:
    """Simple commands of the script (split at ; && || | and new lines), as shell tokens; the code string of an
    interpreter's ``-c`` / ``-e`` is dropped."""
    out: list[list[str]] = []
    for seg in re.split(r"\n|;|&&|\|\||\|", _script_of(command)):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        while toks and re.match(r"^\w+=", toks[0]):          # leading VAR=value assignments
            toks = toks[1:]
        while toks and toks[0] in ("sudo", "env", "time", "nice", "timeout", "xargs", "then", "do", "else", "!", "{", "("):
            toks = toks[1:]
        if not toks:
            continue
        prog = os.path.basename(toks[0])
        if _INTERPRETERS.match(prog):
            kept, skip = [toks[0]], False
            for t in toks[1:]:
                if skip:
                    skip = False
                    continue
                if t in ("-c", "-e"):
                    skip = True
                    continue
                kept.append(t)
            toks = kept
        out.append(toks)
    return out


def _listed_paths(command: str) -> list[str]:
    """Absolute paths given as arguments to a listing command (find, ls, tree, du, rg, grep -r, locate) that ran;
    path literals inside code (heredocs, ``python -c``) or in other commands' arguments are not listing targets."""
    out = []
    for toks in _segments(command):
        prog = os.path.basename(toks[0])
        recursive_grep = prog == "grep" and any(re.match(r"^-[a-zA-Z]*[rR]", t) for t in toks[1:])
        if prog not in _LIST_PROGS and not recursive_grep:
            continue
        for t in toks[1:]:
            if t.startswith("/") and not t.startswith("//"):
                out.append(t)
    return out


_GENERIC_STEMS = {"util", "utils", "core", "base", "main", "types", "typing", "compat", "helpers", "common", "errors",
                  "exceptions", "constants", "config", "settings", "version", "api", "cli", "init", "models", "tests"}


def project_names(run_dir: Path) -> list[str]:
    """Names that identify the task's original project: the project (task id without ``nl2_``), its package (the last
    part of ``source_code`` in the dataset config) and its distinctive module stems (the frozen plan's focus paths;
    common words, standard-library and generic stems such as util or core excluded)."""
    import yaml

    run_dir = Path(run_dir)
    task = run_dir.name
    names = {task.removeprefix("nl2_").lower()}
    for root in (Path("/root/codex-benchmarks/nl2repo/cpe_format"),
                 Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")):
        cfg = root / task / "config.json"
        if cfg.is_file():
            names.add(Path(str(json.loads(cfg.read_text()).get("source_code") or task)).name.removesuffix(".py").lower())
            break
    words: set[str] = set()
    wp = ROOT / "configs" / "memory" / "common_english_words.txt"
    if wp.is_file():
        words = {w.strip().lower() for w in wp.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")}
    std = {n.lower() for n in getattr(__import__("sys"), "stdlib_module_names", ())}
    plan = run_dir / "plan.yaml"
    if plan.is_file():
        for fp in re.findall(r"^\s*-\s*(\S+\.py)\s*$", plan.read_text(encoding="utf-8", errors="replace"), re.M):
            stem = Path(fp).stem.strip("_").lower()
            if len(stem) >= 5 and stem not in words and stem not in std and stem not in _GENERIC_STEMS:
                names.add(stem)
    return sorted(n for n in names if len(n) >= 3)


def _mentions(text: str, names: list[str]) -> bool:
    low = text.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low) or f"*{n}" in low or f"{n}*" in low for n in names)


_HEREDOC_AT = re.compile(r"(?m)^([^\n]*?)<<-?\s*(['\"]?)(\w+)\2[^\n]*\n(.*?)\n[ \t]*\3[ \t]*(?=\n|$)", re.S)
_PACKAGING = {"pyproject.toml", "setup.py", "setup.cfg"}
# a script that runs code it builds at run time is not a plain file editor: its string literals may be executed
_DYNAMIC = re.compile(r"\b(?:exec|eval|compile|__import__|import_module|run_module|run_path)\s*\(|\bsubprocess\b|\bos\.(?:system|popen|exec\w*)\b")
_WRITES = re.compile(r"\bopen\s*\([^)]*['\"][wa]\+?['\"]|\.write_text\s*\(|\.write\s*\(")


def _own_package_path(p: str, names: list[str], own_batch: str) -> bool:
    """A path inside the package being written (or one of its packaging files), relative to the workspace or
    absolute inside the run."""
    p = p.strip("'\"")
    if p.startswith("/"):
        if not (own_batch and p.startswith(own_batch.rstrip("/") + "/")):
            return False
        p = p[len(own_batch.rstrip("/")) + 1:]
        parts = p.split("/")
        return ".." not in parts and (parts[-1] in _PACKAGING or any(c.lower() in names for c in parts[:-1]))
    if p.startswith(("~", "$")) or ".." in p.split("/"):
        return False
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if parts[:1] == ["src"]:
        parts = parts[1:]
    return bool(parts) and ((len(parts) == 1 and parts[0] in _PACKAGING) or (len(parts) > 1 and parts[0].lower() in names))


def _written_text_removed(cmd: str, names: list[str], own_batch: str, query: str, alt: str) -> str:
    """The command with the text it only writes into the package being written blanked out (2026-11-05 user
    decision: a package's own source that reads its own version is not a query; a run that performs the query still
    is). Two forms: a heredoc body redirected into a package file (cat > f, cat >> f, tee f), and, in a Python file
    editor fed by a heredoc, the string literals that hold the query -- only when the editor writes a file, names a
    package path, and runs no dynamically built code. Executed code (a direct call) is never blanked."""
    import io
    import tokenize

    hit = re.compile(rf"\b{query}\s*\(\s*['\"]({alt})['\"]", re.I)

    def repl(m: re.Match) -> str:
        head, body = m.group(1), m.group(4)
        if not hit.search(body):
            return m.group(0)
        w = re.search(r"\b(?:cat\s*>>?|tee(?:\s+-a)?)\s*(\S+)", head)
        if w and _own_package_path(w.group(1), names, own_batch):
            return m.group(0).replace(body, "")
        prog = head.split()[0] if head.split() else ""
        if _INTERPRETERS.match(os.path.basename(prog)) and prog.startswith("python") and not _DYNAMIC.search(body) \
                and _WRITES.search(body):
            try:
                toks = list(tokenize.generate_tokens(io.StringIO(body + "\n").readline))
            except (tokenize.TokenError, SyntaxError, IndentationError):
                return m.group(0)
            strings = [t.string for t in toks if t.type == tokenize.STRING]
            if not any(_own_package_path(s, names, own_batch) for s in strings):
                return m.group(0)
            new = body
            for s in strings:
                if hit.search(s):
                    new = new.replace(s, "''")
            return m.group(0).replace(body, new)
        return m.group(0)

    return _HEREDOC_AT.sub(repl, cmd)


def _original_project_search(cmd: str, names: list[str], own_batch: str) -> str:
    """Outside the workspace, by the project's or its modules' name (returns the kind, "" when none): a listing /
    search with an absolute path outside the run; a query of installed distributions (pip show / download / install
    <name>, pip list | grep <name>, importlib.metadata, pkg_resources, importlib.util.find_spec); an import run from a
    directory outside the run or with -I / -S, unless the command puts its own copy on the path (PYTHONPATH= or
    sys.path) -- importing the package being written is normal wherever it is run."""
    if not names:
        return ""
    alt = "|".join(map(re.escape, names))

    def outside(p: str) -> bool:
        # the run's own directories, and the agent's own scratch areas (/tmp, $TMPDIR: its own temporary build
        # directories carry the project's name), are not "outside the workspace"
        if p.startswith(("/tmp", "$TMPDIR", "${TMPDIR")):
            return False
        return p.startswith("/") and not (own_batch and (p == own_batch or p.startswith(own_batch.rstrip("/") + "/")))
    script = _script_of(cmd)
    # the command builds or installs its own package, or puts its own copy on the path: metadata queries and imports
    # then look at that copy
    own_copy = bool(re.search(r"PYTHONPATH=\S|sys\.path\.(?:insert|append)|--target\b|\bpip[\d.]*\s+(?:wheel|install)\b[^\n]*\s\.(?:\s|$)"
                              r"|\.whl\b|-m\s+build\b", cmd))
    for toks in _segments(cmd):
        prog = os.path.basename(toks[0])
        grep_r = prog == "grep" and any(re.match(r"^-[a-zA-Z]*[rR]", t) for t in toks[1:])
        # the name must be in what is searched for (patterns, terms), not in a directory searched in: the task's own
        # environment directory (envs/nl2_<task>) carries the name
        if (prog in _LIST_PROGS or grep_r) and any(outside(t) for t in toks[1:]) and \
                _mentions(" ".join(t for t in toks[1:] if not t.startswith("/")), names):
            return "search outside the workspace"
        if re.match(r"^pip[\d.]*$", prog) or (re.match(r"^python[\d.]*$", prog) and toks[1:3] == ["-m", "pip"]):
            args = toks[1:] if prog.startswith("pip") else toks[3:]
            if args and args[0] in ("show", "download", "install") and any(t.lower() in names for t in args[1:]):
                return "installed-distribution query"
    if re.search(r"\bpip[\d.]*\s+(?:list|freeze)\b[^\n]*\|\s*grep\b", script) and \
            _mentions(" ".join(re.findall(r"\|\s*grep\b([^\n|;&]*)", script)), names):
        return "installed-distribution query"
    # importlib.metadata / pkg_resources under any alias (import importlib.metadata as m; m.version("x"))
    meta = bool(re.search(r"importlib\.metadata|importlib_metadata|pkg_resources|from\s+importlib\s+import\s+[^\n]*\bmetadata\b", cmd))
    query = (r"(?:(?:\w+\.)?(?:version|distribution|files|metadata|requires)|get_distribution|find_distributions?|find_spec)"
             if meta else r"(?:find_spec|get_distribution)")
    # a cd to a directory outside the run makes relative package paths someone else's: nothing is blanked then
    cd_out = any(os.path.basename(t[0]) == "cd" and len(t) > 1 and outside(t[1]) for t in _segments(cmd))
    run = cmd if cd_out else _written_text_removed(cmd, names, own_batch, query, alt)
    if not own_copy and re.search(rf"\b{query}\s*\(\s*['\"]({alt})['\"]", run, re.I):
        return "installed-distribution query"
    m = re.search(rf"\b{query}\s*\(\s*(\w+)\s*\)", run)
    if not own_copy and m and re.search(rf"for\s+{m.group(1)}\s+in\s*[\[(][^\])]*['\"]({alt})['\"]", run, re.I):
        return "installed-distribution query"
    cwd_out = False
    for toks in _segments(cmd):
        prog = os.path.basename(toks[0].lstrip("("))
        if prog == "cd" and len(toks) > 1:
            cwd_out = outside(toks[1])
        elif _INTERPRETERS.match(prog):
            isolated = "-I" in toks[1:3] or "-S" in toks[1:3]
            if (cwd_out or isolated) and not own_copy and re.search(
                    rf"\bimport\s+({alt})\b|\bfrom\s+({alt})\s+import\b", cmd):
                return "import from outside the workspace"
    return ""


CANARY_RE = re.compile(r"CANARY-PL-[0-9a-f]{16}")


def _captures(run_dir: Path) -> list[Path]:
    m = Path(run_dir) / "sandbox_manifest.json"
    if not m.is_file():
        return []
    return [Path(e["capture"]) for e in json.loads(m.read_text(encoding="utf-8")).get("entries") or [] if e.get("capture")]


def _captured(f: Path) -> list[dict[str, Any]]:
    out = []
    if f.is_file():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            out.append({"role": str(d.get("role") or f.parent.name), "command": str(d.get("command") or ""),
                        "output": str(d.get("output") or ""), "exit_code": d.get("exit_code"), "source": str(f)})
    return out


def _trajectory_commands(path: Path) -> int:
    """Shell commands a mini-swe-agent trajectory executed (request_budget is answered by the driver, not the shell)."""
    if not path.is_file():
        return 0
    try:
        msgs = json.loads(path.read_text(encoding="utf-8", errors="replace")).get("messages") or []
    except ValueError:
        return 0
    n = 0
    for m in msgs:
        for a in ((m.get("extra") or {}).get("actions") or []) if isinstance(m, dict) else []:
            if not str(a.get("command") or "").lstrip().startswith("request_budget"):
                n += 1
    return n


def agent_visible_files(run_dir: Path) -> list[Path]:
    """What agents read or wrote: captures, trajectories, workspaces and repair evidence of the manifest's sessions."""
    m = Path(run_dir) / "sandbox_manifest.json"
    if not m.is_file():
        return []
    files: set[Path] = set()
    for e in json.loads(m.read_text(encoding="utf-8")).get("entries") or []:
        for k in ("capture", "trajectory"):
            if e.get(k) and Path(e[k]).is_file():
                files.add(Path(e[k]))
        ws = Path(str(e.get("workspace") or ""))
        for d in (ws, ws.parent / "repair_evidence"):
            if str(d) not in ("", ".") and d.is_dir():
                files |= {p for p in d.rglob("*") if p.is_file() and not p.is_symlink()}
    return sorted(files)


def canary_hits(run_dir: Path) -> list[str]:
    hits = []
    for p in agent_visible_files(run_dir):
        try:
            if p.stat().st_size <= 20_000_000 and CANARY_RE.search(p.read_text(encoding="utf-8", errors="ignore")):
                hits.append(str(p))
        except OSError:
            continue
    return hits


def calls(run_dir: Path) -> list[dict[str, Any]]:
    """Every captured agent command with its output, role and source: the mini-swe-agent captures, then Codex turn
    items, then Codex session logs."""
    run_dir = Path(run_dir)
    out: list[dict[str, Any]] = []
    for f in _captures(run_dir):
        out += _captured(f)
    have_items: set[str] = set()
    for f in sorted(run_dir.rglob("*.items.json")):
        if "backend_traces" not in f.parts:
            continue
        role = f.parent.name
        have_items.add(str(f.parent))
        try:
            items = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("type") == "commandExecution":
                out.append({"role": role, "command": str(it.get("command") or ""), "output": str(it.get("aggregated_output") or ""),
                            "exit_code": it.get("exit_code"), "source": str(f)})
    for f in sorted(run_dir.rglob("*.jsonl")):
        if "codex_home" not in f.parts or "sessions" not in f.parts:
            continue
        node_dir = str(f).split("/.sandbox/")[0]
        if node_dir in have_items:
            continue
        role = Path(node_dir).name
        pending: dict[str, dict[str, Any]] = {}
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                pl = (json.loads(line).get("payload") or {})
            except ValueError:
                continue
            if pl.get("type") == "function_call":
                cmds = A.commands_from_session(json.dumps({"payload": pl}))
                pending[str(pl.get("call_id"))] = {"role": role, "command": cmds[0] if cmds else "", "output": "",
                                                   "exit_code": None, "source": str(f)}
            elif pl.get("type") == "function_call_output" and str(pl.get("call_id")) in pending:
                o = pl.get("output")
                pending[str(pl.get("call_id"))]["output"] = o if isinstance(o, str) else json.dumps(o)
        out += list(pending.values())
    return out


def agent_calls(run_dir: Path) -> list[dict[str, Any]]:
    """Agent calls the manifest records (one entry per confined Codex start)."""
    m = Path(run_dir) / "sandbox_manifest.json"
    if not m.is_file():
        return []
    return [e for e in json.loads(m.read_text(encoding="utf-8")).get("entries") or [] if e.get("kind") == "agent"]


def classify_call(c: dict[str, Any], *, allowed: list[str], own_batch: str = "",
                  names: list[str] | tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """(event, target) pairs for one command and its output."""
    cmd, out = c.get("command") or "", c.get("output") or ""
    ws = ""
    ev: list[tuple[str, str]] = []
    kind = _original_project_search(cmd, list(names), own_batch) if names else ""
    if kind:
        ev.append(("original_project_search", kind))
    kinds = A.classify(cmd, own_batch=own_batch)
    if "ld_preload_tamper" in kinds:
        ev.append(("ld_preload_tamper", ""))
    if _SANDBOX_TAMPER.search(cmd):
        ev.append(("sandbox_tamper", ""))
    if _FETCH.search(cmd) or "package_install" in kinds:
        ev.append(("package_fetch", ""))
    if _NETGUARD.search(out) or ("external_network" in kinds and re.search(r"refused|PermissionError|Errno 13|Errno 111", out)):
        ev.append(("network_denied", ""))
    elif "external_network" in kinds:
        ev.append(("unblocked_breach", "external network"))
    # a refusal fails the command; "Permission denied" inside a file that was read successfully is content
    # ... unless the refused path is an argument of a command that ran (mktemp -d /tmp/x refused inside a chain that
    # still exits 0)
    refused = {(m.group(1) or m.group(2) or "").rstrip("\u2019'\"") for m in _DENIED.finditer(out)}
    if c.get("exit_code") in (0, "0"):
        args = [part for toks in _segments(cmd) for t in toks[1:] for part in re.split(r"[<>=]", t) if part.startswith("/")]
        refused = {d for d in refused if d and any(
            d == a or d.startswith(a.rstrip("/") + "/") or a.startswith(d.rstrip("/") + "/")
            or (os.path.dirname(a) == os.path.dirname(d) and os.path.dirname(d) not in ("/", "")) for a in args)}
    denied = refused
    targets = [p for p in _listed_paths(cmd) if _outside(p, allowed, ws)]
    if targets:
        for t in sorted(set(targets)):
            ev.append(("list_denied", _dir_only(t)))
    for d in sorted(x for x in denied if x):
        if _outside(d, allowed, ws):
            ev.append(("read_denied", _dir_only(d)))
    if denied and not any(e[0] in ("read_denied", "list_denied") for e in ev):
        ev.append(("read_denied", ""))
    # out of bounds and not refused: a reading command on a forbidden path that exited 0 with output and no denial
    forb = [k for k in kinds if k.startswith("forbidden_path")]
    if forb and not denied and _READ_VERB.search(cmd) and c.get("exit_code") in (0, "0") and out.strip() \
            and any(_outside(p, allowed, ws) and os.path.exists(p) for p in _paths_in(cmd)):
        ev.append(("unblocked_breach", forb[0].split(":", 1)[-1]))
    return ev


def _primary_names(run_dir: Path) -> set[str]:
    """The project's own name and its package name (not the module stems)."""
    task = Path(run_dir).name
    out = {task.removeprefix("nl2_").lower()}
    cfg = Path("/root/codex-benchmarks/nl2repo/cpe_format") / task / "config.json"
    if cfg.is_file():
        out.add(Path(str(json.loads(cfg.read_text()).get("source_code") or task)).name.removesuffix(".py").lower())
    return {n.replace("-", "_") for n in out}


def _venvs(allowed: list[str]) -> list[Path]:
    return sorted({Path(r) for r in allowed if (Path(r) / "pyvenv.cfg").is_file()})


def installed_copies(run_dir: Path, allowed: list[str] | None = None) -> list[str]:
    """Copies of the project in the run's granted environments (package directory or module, dist-info / egg-info,
    a .pth or .egg-link naming it). Empty is what makes a refused search harmless: there was nothing to find."""
    allowed = _allowed_roots(run_dir) if allowed is None else allowed
    names = _primary_names(run_dir)
    found = []
    for v in _venvs(allowed):
        for sp in v.glob("lib/python*/site-packages"):
            for n in names:
                for pat in (n, f"{n}.py", f"{n}-*.dist-info", f"{n}-*.egg-info"):
                    found += [str(p) for p in sp.glob(pat)]
            for f in [*sp.glob("*.pth"), *sp.glob("*.egg-link")]:
                try:
                    if any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", f.read_text(errors="replace").lower())
                           for n in names):
                        found.append(str(f))
                except OSError:
                    continue
    return sorted(set(found))


_NOT_FOUND = re.compile(r"PackageNotFoundError|No package metadata was found|ModuleNotFoundError|No module named|"
                        r"Package\(s\) not found|^None$", re.M)


def search_got_content(cmd: str, output: str, kind: str, *, names: list[str], run_root: str,
                       venvs: list[Path]) -> bool:
    """Whether an ``original_project_search`` command obtained anything from outside the run (2026-11-07 user
    decision: refused and verified empty -> CRITICAL, flagged, run kept; anything obtained -> void). Conservative:
    whatever cannot be verified from the output counts as obtained.

    - an absolute path outside the run (and outside the task environment, verified to hold no copy) naming the
      project or a module of it;
    - a distribution query: a printed version, ``pip show`` fields, or no "not found" answer at all;
    - a search: a listing program (ls, tree, du) run on an outside path -- its output names no paths, so it cannot
      be told apart from the run's own;
    - an import from outside: no import error in the output."""
    run_root = run_root.rstrip("/")
    env = [str(v).rstrip("/") + "/" for v in venvs]
    for m in re.finditer(r"(/[^\s'\":,()\[\]<>=]+)", output or ""):
        p = m.group(1)
        if p.startswith(("/tmp", run_root + "/")) or p == run_root or any(p.startswith(e) for e in env):
            continue
        if _mentions(p, names):
            return True
    out = output or ""
    if kind == "installed-distribution query":
        if re.search(r"^\s*v?\d+(?:\.\d+)+\S*\s*$", out, re.M) or re.search(r"^(?:Version|Location|Name):", out, re.M):
            return True
        return not _NOT_FOUND.search(out) and "ModuleSpec(" not in out
    if kind == "search outside the workspace":
        for toks in _segments(cmd):
            if os.path.basename(toks[0]) in ("ls", "tree", "du") and \
                    any(t.startswith("/") and not t.startswith(("/tmp", run_root + "/")) for t in toks[1:]):
                return True
        return False
    if kind == "import from outside the workspace":
        return not re.search(r"ModuleNotFoundError|ImportError|No module named", out)
    return True


def scan_run(run_dir: Path, *, run_id: str = "", write: bool = True) -> dict[str, Any]:
    """Classify a finished run, write its breach log and the global index; returns the summary."""
    run_dir = Path(run_dir)
    run_id = run_id or str(run_dir)
    allowed = _allowed_roots(run_dir)
    names = project_names(run_dir)
    events: list[dict[str, Any]] = []
    cs = calls(run_dir)

    def add(role: str, event: str, target: str, command: str, **extra: Any) -> None:
        events.append({"time": _now(), "run_id": run_id, "role": role, "event": event, "level": LEVEL[event],
                       "target": target, "command": command[:200], **extra})

    copies = installed_copies(run_dir, allowed)
    venvs = _venvs(allowed)
    seen = set()
    for c in cs:
        for event, target in classify_call(c, allowed=allowed, own_batch=str(run_dir.parent), names=names):
            key = (c["role"], event, target, c["command"][:200])
            if key not in seen:
                seen.add(key)
                extra = {}
                if event == "original_project_search":
                    # rule 6 (2026-11-08): did the search obtain anything? a copy in a granted environment counts
                    extra["obtained"] = bool(copies) or search_got_content(
                        c["command"], c.get("output") or "", target, names=list(names), run_root=str(run_dir), venvs=venvs)
                add(c["role"], event, target, c["command"], **extra)
    for e in agent_calls(run_dir):
        if e.get("capture"):
            # a mini-swe-agent session: every command its trajectory executed must be in the capture
            ran, got = _trajectory_commands(Path(str(e.get("trajectory") or ""))), len(_captured(Path(e["capture"])))
            if ran > got:
                add(str(e.get("role") or "-"), "capture_empty", "", f"session {e.get('request_id')}: {ran} commands, {got} captured")
            continue
        # per call: its turn items or its session log must exist, else the call cannot be audited
        rid = str(e.get("request_id") or "")
        priv = Path(str(e.get("private_dir") or ""))
        has_session = (bool(str(priv)) and priv.is_dir() and any(priv.rglob("rollout-*.jsonl"))) or \
            (bool(rid) and any(run_dir.rglob(f".sandbox/{rid}/codex_home/sessions/**/rollout-*.jsonl")))   # run moved since
        has_items = bool(rid) and any(run_dir.rglob(f"{rid}.items.json"))
        if not has_session and not has_items:
            add(str(e.get("node_id") or e.get("role") or "-"), "capture_empty", "", f"agent call {rid or '?'}: no turn items, no session log")
    for hit in canary_hits(run_dir):
        add("-", "canary", _dir_only(hit), "planner-state canary found in an agent-visible file")
    if write:
        log = run_dir / LOG_NAME
        with log.open("w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        with INDEX.open("a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    s = summary_of(events, runs=1, commands=len(cs))
    s["run_id"] = run_id
    crit = [e for e in events if e["level"] == "CRITICAL"]
    # rule 6 (2026-11-08, as AdaMAS): a refused search verified to have obtained nothing is CRITICAL and flagged, the
    # run kept -- verified = no content in the output, no copy of the project in a granted environment, and the
    # final code's similarity to the reference not an outlier; anything else voids the run
    kept = [e for e in crit if e["event"] == "original_project_search" and e.get("obtained") is False]
    s["void"] = len(crit) > len(kept)
    s["flagged"] = bool(kept) and not s["void"]
    if s["flagged"]:
        sim = similarity_check(run_dir) if similarity_check else {"ok": False, "why": "no similarity check"}
        s["similarity"] = sim
        if not sim.get("ok"):
            s["void"], s["flagged"] = True, False
    s["manual_review"] = s["warning"] >= REVIEW_WARNINGS
    if write and s["void"]:
        mark_void(run_dir, run_id, crit)
    if write and s["flagged"]:
        (run_dir / "SANDBOX_FLAGGED").write_text(json.dumps({"critical": kept, "similarity": s["similarity"]}, indent=1),
                                                 encoding="utf-8")
    return s


def _similarity_of_final(run_dir: Path) -> dict[str, Any]:
    """The sealed similarity check of the run's final code against the finals of the task's other rounds."""
    import subprocess
    import sys

    run_dir = Path(run_dir)
    final, task = run_dir / "final", run_dir.name
    if not final.is_dir():
        return {"ok": False, "why": "no final code"}
    pop = [str(p) for p in sorted(run_dir.parent.parent.glob(f"*/{task}/final")) if p != final and not is_void(p.parent)]
    script = ROOT / "scripts" / "sealed" / "reference_similarity.py"
    try:
        p = subprocess.run([sys.executable, str(script), "check", task, str(final), "--population", *pop],
                           capture_output=True, text=True, timeout=900, check=False)
        r = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:                                   # cannot verify: not kept
        return {"ok": False, "why": f"similarity check failed: {type(e).__name__}"}
    return {"ok": not r["outlier"], **r}


# the similarity check the flag needs (replaceable in the positive control); None: never kept
similarity_check = _similarity_of_final


def summary_of(events: Iterable[dict[str, Any]], *, runs: int, commands: int) -> dict[str, Any]:
    evs = list(events)
    warn = [e for e in evs if e["level"] == "WARNING"]
    crit = [e for e in evs if e["level"] == "CRITICAL"]
    return {"runs": runs, "commands": commands, "warning": len(warn), "critical": len(crit),
            "warning_by_event": dict(Counter(e["event"] for e in warn)), "critical_events": crit}


def mark_void(run_dir: Path, run_id: str, critical: list[dict[str, Any]]) -> None:
    VOID.parent.mkdir(parents=True, exist_ok=True)
    with VOID.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"time": _now(), "run_id": run_id, "run_dir": str(Path(run_dir).resolve()),
                             "critical": len(critical)}) + "\n")
    (Path(run_dir) / "SANDBOX_VOID").write_text(json.dumps(critical, indent=1), encoding="utf-8")


def void_runs() -> set[str]:
    if not VOID.is_file():
        return set()
    out = set()
    for line in VOID.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["run_dir"])
        except (ValueError, KeyError):
            continue
    return out


def is_void(run_dir: str | Path, _cache: dict[str, Any] = {}) -> bool:   # noqa: B006 -- per-process cache
    """A run with a CRITICAL sandbox event: its results count nowhere."""
    if not run_dir:
        return False
    mtime = VOID.stat().st_mtime if VOID.is_file() else 0.0
    if _cache.get("mtime") != mtime:
        _cache.update(mtime=mtime, runs=void_runs())
    rd = str(Path(run_dir).resolve())
    return any(rd == v or rd.startswith(v.rstrip("/") + "/") or v.startswith(rd.rstrip("/") + "/") for v in _cache["runs"])


def report_section(summaries: list[dict[str, Any]]) -> str:
    """The "沙盒越界警告" section every task report carries (format: docs/sandbox_breach_log.md)."""
    runs = sum(s.get("runs", 1) for s in summaries)
    cmds = sum(s.get("commands", 0) for s in summaries)
    warn = Counter()
    for s in summaries:
        warn.update(s.get("warning_by_event") or {})
    crit = [e for s in summaries for e in s.get("critical_events") or []]
    review = [s["run_id"] for s in summaries if s.get("manual_review")]
    lines = ["## 沙盒越界警告", f"- 运行数：{runs}；命令总数：{cmds}",
             f"- WARNING：{sum(warn.values())} 条" + ("（" + "，".join(f"{k} {v}" for k, v in sorted(warn.items())) + "）" if warn else "")]
    lines.append(f"- CRITICAL：{len(crit)} 条" + ("" if crit else ""))
    for e in crit:
        lines.append(f"  - {e['run_id']} | {e['role']} | {e['event']} | {e['command'][:120]}")
    if review:
        lines.append("- 需人工复核（WARNING ≥ 5）：" + "，".join(review))
    if crit:
        lines.append("- 结论：有越界（以上运行作废，不计入任何统计）")
    elif warn:
        lines.append("- 结论：无越界成功的命令；WARNING 均被拦截，逐条见各运行的 sandbox_breach.log")
    else:
        lines.append("- 结论：无越界")
    return "\n".join(lines)


__all__ = ["INDEX", "LEVEL", "LOG_NAME", "VOID", "agent_calls", "calls", "classify_call", "is_void", "mark_void",
           "report_section", "scan_run", "summary_of", "void_runs"]
