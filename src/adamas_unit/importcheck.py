"""The closing import check and its one bounded fix (patch 2026-11-09; DESIGN §7.1). The same for every arm.

Why: on deepdiff, most of the feedback half's spread came from whole held-out files that collect nothing because a
documented name was not importable (docs/reports/heldout_variance_2026-11-09.md). This takes that noise out of the
budget experiments the same way for every arm.

1. **Manifest** (``build_manifest``), from the design documents alone (``docs/`` of the dataset repository; never
   the held-out suite, the reference implementation or a sealed file), once per task before any round, frozen with
   its hash under ``outputs/importcheck/<task>/manifest.json`` and shared by all arms:
   - names with a module: the documents' import statements (``from pkg.mod import a, b`` and the parenthesised,
     multi-line form), the ``# in mod.py`` sections of their code blocks, and AdaMAS ``public_symbols``' attributed
     names (as the submittable check);
   - names without a module: the documents' API headings (``### 40.get_type() Function``, ``### 160.AnySet Class``)
     not attributed above; checked at the package top level (``unattributed: top_level``) or only recorded
     (``record``);
   - modules under test / benchmark directories and test_*.py / *_test.py / conftest.py are left out (submit_check);
   - command-line entries: ``python -m <pkg>`` and console-script lines (``name = mod:func``) in the documents.
   Each name belongs to the one unit whose focus paths hold its module; a module in no unit's or in several units'
   focus paths (``__init__.py`` is in U1's and U5's), and every name without a module, is checked only at the end.
2. **Check** (``check``): no model call. In the sandbox, on a copy of the code (never in the agent's directory):
   every manifest name in scope imports; every module of the package in scope imports; every ``__all__`` name
   exists; every documented command-line entry prints its help.
3. **Fix** (``fix``): only before the final submission (user decision 2026-11-09: one fix per round for every arm;
   the unit-end checks are recorded, never fixed). When the final check finds something, one session (``importfix.max_sessions``) with a fixed cap
   (``importfix.tokens`` / ``importfix.seconds``) on a ledger of its own: nothing is charged to the round ledger,
   the planner never sees it, later units' shares do not change. Input: the missing names (from the documents),
   the documents' lines about them, the code. Then a mechanical scope check (``scope_check``): only imports,
   exports, ``__all__`` and new definitions of the missing names; no existing function or class changed; at most
   ``importfix.max_lines`` lines changed. Anything else: the code before the fix is restored. One re-check;
   never a second fix.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adamas_unit.submit_check import _is_test_module, documented_names
from orchestra.codeprojecteval.public_symbols import parse_directory_tree
from orchestra.sandbox.guard import require_sandbox, run_confined

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "outputs" / "importcheck"
DEFAULTS: dict[str, dict[str, Any]] = {
    "importcheck": {"enabled": True, "per_unit": True, "unattributed": "record"},
    "importfix": {"tokens": 150_000, "seconds": 600, "max_sessions": 1, "max_lines": 150},
}
_SKIP = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc")


def load_config(path: Path | None = None) -> dict[str, dict[str, Any]]:
    import yaml

    p = Path(path) if path else ROOT / "configs" / "importcheck.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return {k: {**v, **((raw or {}).get(k) or {})} for k, v in DEFAULTS.items()}


# ---------------------------------------------------------------------------------------------- 1. the manifest

_FENCE = re.compile(r"```[\w+-]*\n(.*?)```", re.S)
_FROM = re.compile(r"(?:^|>>>\s*|\.\.\.\s*)from\s+([\w.]+)\s+import\s+(\([^)]*\)|[^\n#]+)", re.M)
_IN_FILE = re.compile(r"^#\s*in\s+([\w/.-]+\.py)\s*$")
_TOP_DEF = re.compile(r"^(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")
_TOP_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)")
_HEADING = re.compile(r"^#{2,6}\s*\d+\.\s*`?([A-Za-z_]\w*)`?(\(\))?\s+(Function|Class)\b")
_CONSOLE = re.compile(r"^\s*([A-Za-z][\w-]*)\s*=\s*\"?([\w.]+):(\w+)\"?\s*$", re.M)


def manifest_inputs(task) -> list[Path]:
    """The only files the manifest reads: the design documents under the dataset repository's ``docs/``."""
    d = Path(task.repo_root) / "docs"
    return sorted(p for p in d.glob("*") if p.is_file() and p.suffix.lower() in (".md", ".txt", ".rst"))


def _module_of_file(path: str, tree: list[str], package: str) -> str:
    """``helper.py`` / ``deepdiff/helper.py`` -> ``deepdiff.helper`` (by the documents' directory tree)."""
    p = path.strip().lstrip("./")
    hits = [t for t in tree if t.endswith(".py") and (t == p or t.endswith("/" + p))]
    hits = [h.split("/", 1)[1] if h.split("/")[0] not in (package,) and "/" in h and h.split("/", 1)[1].startswith(package + "/") else h
            for h in hits]
    hits = sorted({h for h in hits if h.split("/")[0] == package}, key=lambda h: h.count("/"))
    if not hits and p.split("/")[0] == package:
        hits = [p]
    if not hits and "/" not in p:
        hits = [f"{package}/{p}"]
    if not hits:
        return ""
    return hits[0].removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def _file_of_module(mod: str) -> str:
    return mod.replace(".", "/") + ".py"


def build_manifest_from_docs(docs: dict[str, str], package: str, units: list[tuple[str, list[str]]]) -> dict[str, Any]:
    """Documents (file name -> text) -> the manifest. ``units``: (unit id, focus paths) in plan order."""
    tree: list[str] = []
    for fname, text in docs.items():
        if "directory_tree" in fname or "architecture" in fname.lower():
            tree += parse_directory_tree(text)
    tree = sorted(set(tree))
    names: dict[tuple[str, str], str] = {}                # (module, name) -> source

    def add(mod: str, name: str, source: str) -> None:
        if not mod or mod.split(".")[0] != package or not re.match(r"^[A-Za-z_]\w*$", name):
            return
        mfile = _file_of_module(mod) if mod != package else f"{package}/__init__.py"
        if _is_test_module(mfile):
            return
        names.setdefault((mod, name), source)

    allcode = "\n".join(m.group(1) for t in docs.values() for m in _FENCE.finditer(t))
    for mod, body in _FROM.findall(allcode):
        body = body.strip().strip("()")
        for part in body.replace("\n", " ").split(","):
            n = part.strip().split(" as ")[0].strip()
            if n and n != "*":
                add(mod, n, "import_statement")
    for text in docs.values():
        for m in _FENCE.finditer(text):
            current = ""
            for line in m.group(1).splitlines():
                f = _IN_FILE.match(line.strip())
                if f:
                    current = _module_of_file(f.group(1), tree, package)
                    continue
                if not current or line[:1].isspace():
                    continue
                d = _TOP_DEF.match(line) or _TOP_ASSIGN.match(line)
                if d and not d.group(1).startswith("__"):
                    add(current, d.group(1), "module_comment")
    for mod, n in documented_names(docs, package):
        add(mod, n, "public_symbols")
    attributed = {n for (_, n) in names}
    unattributed = set()
    for text in docs.values():
        for line in text.splitlines():
            h = _HEADING.match(line)
            if h and not h.group(1).startswith("_") and h.group(1) not in attributed:
                unattributed.add(h.group(1))
    alltext = "\n".join(docs.values())
    cli = []
    if re.search(rf"python3?\s+-m\s+{re.escape(package)}\b", alltext):
        cli.append({"kind": "module", "entry": package})
    for name, mod, func in _CONSOLE.findall(allcode):
        if mod.split(".")[0] == package:
            cli.append({"kind": "console_script", "entry": f"{mod}:{func}", "name": name})

    def unit_of(mod: str) -> int | None:
        f = _file_of_module(mod) if mod != package else f"{package}/__init__.py"
        owners = [i for i, (_, focus) in enumerate(units) if any(f == p.strip("/") or f.startswith(p.strip("/") + "/") for p in focus)]
        return owners[0] if len(owners) == 1 else None

    entries = [{"name": n, "module": m, "source": s, "unit": unit_of(m)} for (m, n), s in sorted(names.items())]
    entries += [{"name": n, "module": "", "source": "api_heading", "unit": None} for n in sorted(unattributed)]
    modules = sorted({e["module"] for e in entries if e["module"]})
    body = {"package": package, "names": entries, "modules": modules, "cli": cli,
            "module_units": {m: unit_of(m) for m in modules}}
    return body


def build_manifest(task, *, out_dir: Path | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Build and freeze the task's manifest (once, before any round). Refuses to change a frozen one."""
    inputs = manifest_inputs(task)
    docs = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in inputs}
    body = build_manifest_from_docs(docs, task.package, [(u.unit_id, list(u.focus_paths)) for u in task.units])
    body = {"task": task.task_id, "inputs": [str(p) for p in inputs], **body}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    rec = {**body, "sha256": digest}
    d = Path(out_dir) if out_dir else STORE / task.task_id
    f = d / "manifest.json"
    if f.is_file() and not overwrite:
        old = json.loads(f.read_text(encoding="utf-8"))
        if old.get("sha256") != digest:
            raise RuntimeError(f"{f}: frozen manifest differs from the documents' (frozen {old.get('sha256')}, now {digest})")
        return old
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return rec


def load_manifest(task_id: str, path: Path | None = None) -> dict[str, Any]:
    f = Path(path) if path else STORE / task_id / "manifest.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    body = {k: v for k, v in rec.items() if k != "sha256"}
    if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != rec.get("sha256"):
        raise RuntimeError(f"{f}: manifest hash mismatch")
    return rec


def scope_of(manifest: dict[str, Any], upto_unit: int | None, unattributed: str = "top_level") -> dict[str, Any]:
    """What one check covers. ``upto_unit`` None: the final check (everything)."""
    pkg = manifest["package"]
    names, mods = [], []
    for e in manifest["names"]:
        if not e["module"]:
            if upto_unit is None and unattributed == "top_level":
                names.append([pkg, e["name"]])
            continue
        if upto_unit is None or (e["unit"] is not None and e["unit"] <= upto_unit):
            names.append([e["module"], e["name"]])
    for m, u in manifest["module_units"].items():
        if upto_unit is None or (u is not None and u <= upto_unit):
            mods.append(m)
    return {"names": sorted(map(list, {tuple(x) for x in names})), "modules": sorted(mods),
            "cli": manifest["cli"] if upto_unit is None else [], "all_package_modules": upto_unit is None}


# ---------------------------------------------------------------------------------------------- 2. the check

PROBE = r'''
import importlib, json, pkgutil, sys, os
pkg, scope = sys.argv[1], json.loads(sys.argv[2])
out = {"names": {}, "modules": {}, "all": {}}
def err(e):
    return f"{type(e).__name__}: {e}"[:240]
mods = list(scope["modules"])
if scope.get("all_package_modules"):
    root = os.path.join(os.getcwd(), *pkg.split("."))
    if os.path.isdir(root):
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in ("test", "tests", "testing", "benchmark", "benchmarks", "__pycache__")]
            for f in fn:
                if f.endswith(".py") and not (f.startswith("test_") or f.endswith("_test.py") or f == "conftest.py"):
                    rel = os.path.relpath(os.path.join(dp, f), os.getcwd())[:-3].replace(os.sep, ".")
                    mods.append(rel.removesuffix(".__init__"))
for m in sorted(set(mods)):
    try:
        mod = importlib.import_module(m)
        out["modules"][m] = ""
    except BaseException as e:
        out["modules"][m] = err(e)
        continue
    allv = getattr(mod, "__all__", None)
    if isinstance(allv, (list, tuple)):
        miss = [n for n in allv if isinstance(n, str) and not hasattr(mod, n)]
        if miss:
            out["all"][m] = miss
for m, n in scope["names"]:
    try:
        mod = importlib.import_module(m)
        out["names"][f"{m}:{n}"] = "" if hasattr(mod, n) else "missing"
    except BaseException as e:
        out["names"][f"{m}:{n}"] = "module: " + err(e)
print("IMPORTCHECK " + json.dumps(out))
'''


def _tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(d).rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            h.update(str(p.relative_to(d)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def check(code_dir: Path, *, scope: dict[str, Any], package: str, venv: Path, work_dir: Path,
          timeout: float = 300) -> dict[str, Any]:
    """Run the check on a copy of ``code_dir`` (sandboxed). Returns the problems (names, modules, __all__, CLI)."""
    require_sandbox("import check")
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="importcheck-", dir=work_dir))
    py = str(Path(venv) / "bin" / "python")
    t0 = time.time()
    try:
        cwd = tmp / "code"
        shutil.copytree(code_dir, cwd, symlinks=True, ignore=_SKIP)
        (tmp / "_probe.py").write_text(PROBE, encoding="utf-8")
        env = {"PATH": f"{venv}/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"}

        def run(args: list[str]):
            return run_confined(args, cwd=cwd, env=env, role="importcheck", capture_output=True, text=True,
                                timeout=timeout, check=False)

        try:
            p = run([py, str(tmp / "_probe.py"), package, json.dumps(scope)])
            line = next((ln for ln in p.stdout.splitlines() if ln.startswith("IMPORTCHECK ")), "")
            probe = json.loads(line.removeprefix("IMPORTCHECK ")) if line else {"error": (p.stderr or "")[-400:]}
        except Exception as e:  # noqa: BLE001 -- a timeout or a crash is a finding, never a pass
            probe = {"error": f"{type(e).__name__}: {e}"[:400]}
        cli = []
        for c in scope.get("cli") or []:
            if c["kind"] == "module":
                args = [py, "-m", c["entry"], "--help"]
            else:
                mod, func = c["entry"].split(":")
                args = [py, "-c", f"import sys; sys.argv=[{c.get('name', func)!r}, '--help']; "
                                  f"from {mod} import {func}; {func}()"]
            try:
                ok = run(args).returncode == 0
            except Exception:  # noqa: BLE001
                ok = False
            cli.append({**c, "runs": ok})
        names = probe.get("names") or {}
        missing = sorted(k for k, v in names.items() if v)
        bad_mods = {m: e for m, e in (probe.get("modules") or {}).items() if e}
        problems = len(missing) + len(bad_mods) + sum(len(v) for v in (probe.get("all") or {}).values()) \
            + sum(1 for c in cli if not c["runs"]) + (1 if probe.get("error") else 0)
        return {"names_checked": len(scope["names"]), "names_missing": missing,
                "name_errors": {k: v for k, v in names.items() if v},
                "modules_checked": len(probe.get("modules") or {}), "module_errors": bad_mods,
                "all_missing": probe.get("all") or {}, "cli": cli, "error": probe.get("error", ""),
                "problems": problems, "seconds": round(time.time() - t0, 2)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def allowed_names(result: dict[str, Any]) -> set[str]:
    """The names a fix may define: the missing ones, and those an import error or ``__all__`` names."""
    out = {k.split(":", 1)[1] for k in result.get("names_missing") or []}
    for e in list((result.get("module_errors") or {}).values()) + list((result.get("name_errors") or {}).values()):
        out |= set(re.findall(r"cannot import name '(\w+)'", e)) | set(re.findall(r"has no attribute '(\w+)'", e))
    for v in (result.get("all_missing") or {}).values():
        out |= set(v)
    return out


# ---------------------------------------------------------------------------------------------- 3. the fix

def _files(d: Path) -> dict[str, Path]:
    out = {}
    for p in Path(d).rglob("*"):
        rel = p.relative_to(d)
        if p.is_file() and not ({".git", "__pycache__", ".pytest_cache"} & set(rel.parts)) and p.suffix != ".pyc":
            out[str(rel)] = p
    return out


def _is_all(node: ast.AST) -> bool:
    tg = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
    return isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and all(isinstance(t, ast.Name) and t.id == "__all__" for t in tg)


def _stmt_ok_new(node: ast.AST, allowed: set[str]) -> bool:
    if isinstance(node, (ast.Import, ast.ImportFrom)) or _is_all(node):
        return True
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name in allowed
    if isinstance(node, ast.Assign):
        return all(isinstance(t, ast.Name) and t.id in allowed for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id in allowed
    if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) and isinstance(node.value.value, str):
        return True                                       # a docstring
    if isinstance(node, ast.Try):
        return _guarded_import_ok(node, allowed)
    return False


_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}


def _guarded_import_ok(node: ast.Try, allowed: set[str]) -> bool:
    """A guarded import (user decision 2026-11-11): ``try: <imports> except ImportError: <fallbacks>``, where every
    statement in every block is an import or an assignment to an allowed name; any other logic is out of scope."""
    def simple(st: ast.AST) -> bool:
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            return True
        if isinstance(st, ast.Assign):
            return all(isinstance(t, ast.Name) and t.id in allowed for t in st.targets)
        if isinstance(st, ast.AnnAssign):
            return isinstance(st.target, ast.Name) and st.target.id in allowed
        return False

    def catches_import_error(h: ast.ExceptHandler) -> bool:
        t = h.type
        names = t.elts if isinstance(t, ast.Tuple) else [t]
        return bool(names) and t is not None and all(isinstance(n, ast.Name) and n.id in _IMPORT_ERRORS for n in names)

    return (bool(node.handlers) and all(catches_import_error(h) for h in node.handlers)
            and any(isinstance(st, (ast.Import, ast.ImportFrom)) for st in node.body)
            and all(simple(st) for st in [*node.body, *node.orelse, *node.finalbody, *(x for h in node.handlers for x in h.body)]))


def scope_check(before: Path, after: Path, allowed: set[str], max_lines: int) -> dict[str, Any]:
    """Is the change between two trees an import-only fix? (mechanical, patch §4.5)"""
    bf, af = _files(before), _files(after)
    why: list[str] = []
    lines = 0
    for rel in sorted(set(bf) | set(af)):
        old = bf[rel].read_bytes() if rel in bf else None
        new = af[rel].read_bytes() if rel in af else None
        if old == new:
            continue
        if not rel.endswith(".py"):
            why.append(f"non-Python file changed: {rel}")
            continue
        if new is None:
            why.append(f"file removed: {rel}")
            continue
        if _is_test_module(rel):
            why.append(f"test file changed: {rel}")
            continue
        ot, nt = (old or b"").decode("utf-8", "replace"), new.decode("utf-8", "replace")
        lines += sum(1 for ln in difflib.unified_diff(ot.splitlines(), nt.splitlines(), lineterm="", n=0)
                     if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
        try:
            na = ast.parse(nt)
            oa = ast.parse(ot) if old is not None else ast.Module(body=[], type_ignores=[])
        except SyntaxError as e:
            why.append(f"{rel}: does not parse ({e.msg})")
            continue
        dump = lambda n: ast.dump(n, include_attributes=False)  # noqa: E731
        new_by_dump = Counter(dump(n) for n in na.body)
        # a module may define one name more than once (2026-11-09, tier50-ic-r2: helper.py defined a function twice
        # and the second copy was taken for a change of the first): every old definition must still be there
        new_defs: dict[str, Counter] = {}
        for n in na.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                new_defs.setdefault(n.name, Counter())[dump(n)] += 1
        for n in oa.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if new_defs.get(n.name, Counter())[dump(n)] <= 0:
                    why.append(f"{rel}: existing definition {n.name} changed or removed")
                else:
                    new_defs[n.name][dump(n)] -= 1
            elif isinstance(n, (ast.Import, ast.ImportFrom)) or _is_all(n):
                continue                                  # imports and __all__ may be corrected
            elif new_by_dump[dump(n)] <= 0:
                why.append(f"{rel}: existing statement at line {n.lineno} changed or removed")
            else:
                new_by_dump[dump(n)] -= 1
        old_by_dump = Counter(dump(n) for n in oa.body)
        for n in na.body:
            d = dump(n)
            if old_by_dump[d] > 0:
                old_by_dump[d] -= 1
                continue
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(
                    isinstance(o, type(n)) and o.name == n.name for o in oa.body):
                continue                                  # already reported as a changed definition
            if not _stmt_ok_new(n, allowed):
                why.append(f"{rel}: new statement at line {n.lineno} is not an import, an export or a missing name")
    if lines > max_lines:
        why.append(f"{lines} lines changed, more than {max_lines}")
    return {"ok": not why, "lines": lines, "why": why[:20]}


@dataclass
class FixResult:
    ran: bool
    kept: bool = False
    reverted_because: list[str] = field(default_factory=list)
    lines: int = 0
    tokens: int = 0
    seconds: float = 0.0
    calls: int = 0
    exit_status: str = ""
    before_problems: int = 0
    after_problems: int = 0
    after: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def doc_excerpts(docs: dict[str, str], names: set[str], *, per_name: int = 2, cap: int = 6000) -> str:
    out, used = [], 0
    for n in sorted(names):
        hits = 0
        for fname, text in docs.items():
            ls = text.splitlines()
            for i, line in enumerate(ls):
                if re.search(rf"\b{re.escape(n)}\b", line):
                    block = "\n".join(ls[max(0, i - 1): i + 3])
                    piece = f"[{fname}:{i + 1}] ({n})\n{block}\n"
                    if used + len(piece) > cap:
                        return "\n".join(out)
                    out.append(piece)
                    used += len(piece)
                    hits += 1
                    if hits >= per_name:
                        break
            if hits >= per_name:
                break
    return "\n".join(out)


def problem_text(result: dict[str, Any]) -> str:
    lines = []
    for k in result.get("names_missing") or []:
        m, n = k.split(":", 1)
        why = (result.get("name_errors") or {}).get(k, "")
        lines.append(f"- `from {m} import {n}` fails" + (f" ({why})" if why and why != "missing" else ""))
    for m, e in (result.get("module_errors") or {}).items():
        lines.append(f"- `import {m}` fails: {e}")
    for m, v in (result.get("all_missing") or {}).items():
        lines.append(f"- `{m}.__all__` lists names that do not exist: {', '.join(v)}")
    for c in result.get("cli") or []:
        if not c["runs"]:
            lines.append(f"- the documented command-line entry `{c['entry']}` does not run with --help")
    if result.get("error"):
        lines.append(f"- the check itself failed: {result['error']}")
    return "\n".join(lines[:200])


def fix(*, ws: Path, result: dict[str, Any], docs: dict[str, str], cfg: dict[str, Any], model: Any, venv: Path,
        fix_dir: Path, recheck, agent_config: dict[str, Any], command_timeout: int = 300,
        manifest_dir: Path | None = None) -> FixResult:
    """One fix session on ``ws`` with a ledger of its own; scope check; restore when out of scope; one re-check.
    ``recheck(ws) -> result`` is the same check."""
    from adamas_unit.ledger import Budget, Ledger
    from adamas_unit.sessions import role_prompt, run_session

    fix_dir = Path(fix_dir)
    fix_dir.mkdir(parents=True, exist_ok=True)
    before = fix_dir / "before"
    if before.exists():
        shutil.rmtree(before)
    shutil.copytree(ws, before, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    allowed = allowed_names(result)
    side = Ledger(Budget(tokens=int(cfg["tokens"]), seconds=float(cfg["seconds"]), compressions=0, repairs=0), 1,
                  path=fix_dir / "ledger.json", floor_share=0.0, management_cap_share=0.0, max_repairs_per_unit=0)
    side.open_unit(0)
    lease = side.lease(0, "importfix")
    instance = ("The closing import check found these problems in the repository:\n\n" + problem_text(result)
                + "\n\nWhat the design documents say about these names (the full documents are in `docs/`):\n\n"
                + doc_excerpts(docs, allowed) + "\n\nNames you may define: " + ", ".join(sorted(allowed)))
    margin = min(int(agent_config.get("margin_tokens", 0)), int(cfg["tokens"]) // 5)
    t0 = time.time()
    s = run_session(role="importfix", system=role_prompt("importfix"), instance=instance, workspace=ws, venv=venv,
                    session_dir=fix_dir / "session", ledger=side, lease=lease, model=model, planner=None,
                    summarizer=None, prober=None, agent_config={**agent_config, "margin_tokens": margin, "probe_every": 0},
                    command_timeout=min(command_timeout, int(cfg["seconds"])), manifest_dir=manifest_dir)
    sc = scope_check(before, ws, allowed, int(cfg["max_lines"]))
    fr = FixResult(ran=True, lines=sc["lines"], tokens=int(s.get("used_tokens") or 0), calls=int(s.get("calls") or 0),
                   exit_status=str(s.get("exit_status") or ""), before_problems=int(result["problems"]))
    if sc["ok"]:
        fr.kept = True
    else:
        fr.reverted_because = sc["why"]
        for p in list(Path(ws).iterdir()):
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        for p in before.iterdir():
            (shutil.copytree(p, Path(ws) / p.name, symlinks=True) if p.is_dir() and not p.is_symlink()
             else shutil.copy2(p, Path(ws) / p.name, follow_symlinks=False))
    after = recheck(ws)
    fr.after_problems = int(after["problems"])
    fr.after = {k: after[k] for k in ("names_checked", "names_missing", "module_errors", "all_missing", "problems")}
    fr.seconds = round(time.time() - t0, 1)
    shutil.rmtree(before, ignore_errors=True)
    (fix_dir / "fix.json").write_text(json.dumps({"scope": sc, **fr.to_dict()}, indent=1, default=str), encoding="utf-8")
    return fr


class ImportCheck:
    """What a round holds: the config, the frozen manifest, and the record of every check and fix."""

    def __init__(self, cfg: dict[str, dict[str, Any]], manifest: dict[str, Any], docs: dict[str, str]):
        """``docs``: the design documents as the manifest read them (``manifest_inputs``), not the workspace's copy."""
        self.cfg, self.manifest, self.docs = cfg, manifest, docs
        self.records: list[dict[str, Any]] = []

    @classmethod
    def for_task(cls, task, cfg: dict[str, dict[str, Any]] | None = None) -> "ImportCheck":
        """The task's frozen manifest (built before any round by ``scripts/build_importcheck_manifest.py``)."""
        m = load_manifest(task.task_id)
        docs = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in manifest_inputs(task)}
        if [str(p) for p in manifest_inputs(task)] != m["inputs"]:
            raise RuntimeError(f"{task.task_id}: the documents changed since the manifest was frozen")
        return cls(cfg or load_config(), m, docs)

    @property
    def per_unit(self) -> bool:
        return bool(self.cfg["importcheck"].get("per_unit", True))

    def run(self, *, where: str, upto_unit: int | None, ws: Path, docs: dict[str, str], venv: Path, work_dir: Path,
            model_factory, agent_config: dict[str, Any], command_timeout: int, run_dir: Path,
            allow_fix: bool = True) -> dict[str, Any]:
        """One checkpoint: check; on problems and ``allow_fix`` (the final checkpoint only), one fix and one re-check."""
        t0 = time.time()
        scope = scope_of(self.manifest, upto_unit, self.cfg["importcheck"].get("unattributed", "top_level"))
        pkg = self.manifest["package"]

        def do_check(d: Path) -> dict[str, Any]:
            return check(d, scope=scope, package=pkg, venv=venv, work_dir=Path(work_dir) / "importcheck")

        r = do_check(ws)
        rec: dict[str, Any] = {"where": where, "upto_unit": upto_unit, "before": {k: r[k] for k in (
            "names_checked", "names_missing", "module_errors", "all_missing", "cli", "error", "problems")}, "fixes": []}
        rec["fix_allowed"] = allow_fix
        if allow_fix and r["problems"] and int(self.cfg["importfix"]["max_sessions"]) >= 1:   # one fix, then one re-check; never a second
            fr = fix(ws=ws, result=r, docs=docs, cfg=self.cfg["importfix"], model=model_factory(), venv=venv,
                     fix_dir=Path(work_dir) / "importfix" / where, recheck=do_check, agent_config=agent_config,
                     command_timeout=command_timeout, manifest_dir=run_dir)
            rec["fixes"].append(fr.to_dict())
            r = {**r, **fr.after, "problems": fr.after_problems}
        rec["after_problems"] = r["problems"]
        rec["seconds"] = round(time.time() - t0, 1)
        self.records.append(rec)
        return rec

    def summary(self) -> dict[str, Any]:
        fixes = [f for r in self.records for f in r["fixes"]]
        return {"manifest_sha256": self.manifest["sha256"], "config": self.cfg, "checks": len(self.records),
                "fixes_triggered": len(fixes), "fixes_kept": sum(1 for f in fixes if f["kept"]),
                "fixes_reverted": sum(1 for f in fixes if not f["kept"]),
                "fix_tokens": sum(f["tokens"] for f in fixes), "fix_seconds": round(sum(f["seconds"] for f in fixes), 1),
                "records": self.records}
