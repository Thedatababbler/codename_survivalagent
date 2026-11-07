"""The submittable check: mechanical, observation only (DESIGN §7, USING §2.4).

On a copy of the final code, in the sandbox (workspace policy), with the task venv:

1. the package imports;
2. the public names the documents attribute to a module import from it (AdaMAS ``public_symbols``; top-level names
   only, ``Class.method`` entries are not imported; modules under test / benchmark directories and test_*.py files
   are not the public API and are left out) -- at least ``NAMES_MIN`` of them;
3. every frozen suite collects (``pytest --collect-only``, no collection error);
4. when the documents show ``python -m <package>``, ``python -m <package> --help`` exits 0.

submittable = 1 only when every item holds. No model call, no budget, no repair; the thresholds are fixed here and
neither the planner nor the evolver can change them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from adamas_unit.gate import TESTKIT
from orchestra.codeprojecteval.public_symbols import derive_public_symbols
from orchestra.sandbox.guard import require_sandbox, run_confined

NAMES_MIN = 0.9

PROBE = r'''
import importlib, json, sys
pkg, names = sys.argv[1], json.loads(sys.argv[2])
out = {"package": False, "names": {}}
try:
    importlib.import_module(pkg)
    out["package"] = True
except BaseException as e:
    out["package_error"] = f"{type(e).__name__}: {e}"[:300]
for mod, name in names:
    try:
        m = importlib.import_module(mod)
        out["names"][f"{mod}:{name}"] = hasattr(m, name)
    except BaseException:
        out["names"][f"{mod}:{name}"] = False
print("UNIT_PROBE " + json.dumps(out))
'''


# test and benchmark code is not the package's public API (2026-11-05 user decision: names from these dominated the
# ratio on cerberus, whose docs list the functions of cerberus/benchmarks/test_*.py)
_NOT_API_DIRS = {"test", "tests", "testing", "benchmark", "benchmarks"}


def _is_test_module(module: str) -> bool:
    parts = module.split("/")
    leaf = parts[-1]
    return bool(_NOT_API_DIRS & set(parts[:-1])) or leaf.startswith("test_") or leaf.endswith("_test.py") \
        or leaf == "conftest.py"


def documented_names(docs: dict[str, str], package: str) -> list[tuple[str, str]]:
    out = set()
    for s in derive_public_symbols(docs).symbols:
        if not s.module or "." in s.name or not s.module.endswith(".py") or _is_test_module(s.module):
            continue
        mod = s.module.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        if mod.split(".")[0] == package:
            out.add((mod, s.name))
    return sorted(out)


def check(code_dir: Path, *, package: str, docs: dict[str, str], suites: dict[str, Path], venv: Path,
          work_dir: Path, timeout: float = 600) -> dict[str, Any]:
    require_sandbox("submittable check")
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="submit-", dir=work_dir))
    py = str(Path(venv) / "bin" / "python")
    try:
        cwd = tmp / "code"
        shutil.copytree(code_dir, cwd, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"))
        for uid, src in suites.items():
            shutil.copytree(src, cwd / "_unit_suites" / uid, ignore=shutil.ignore_patterns("__pycache__", "SUITE.json"))
        shutil.copytree(TESTKIT / "adamas_testkit", cwd / "_unit_harness" / "adamas_testkit",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (cwd / "_unit_probe.py").write_text(PROBE, encoding="utf-8")
        env = {"PATH": f"{venv}/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": f"{cwd}:{cwd / '_unit_harness'}",
               "PYTHONDONTWRITEBYTECODE": "1"}
        names = documented_names(docs, package)
        rec: dict[str, Any] = {"package": package, "names_documented": len(names)}

        def run(args: list[str]) -> subprocess.CompletedProcess:
            return run_confined(args, cwd=cwd, env=env, role="submit_check", capture_output=True, text=True, timeout=timeout)

        try:
            p = run([py, "_unit_probe.py", package, json.dumps(names)])
            line = next((ln for ln in p.stdout.splitlines() if ln.startswith("UNIT_PROBE ")), "")
            probe = json.loads(line.removeprefix("UNIT_PROBE ")) if line else {}
        except subprocess.TimeoutExpired:
            probe = {"package": False, "package_error": "import timed out"}
        rec["imports"] = bool(probe.get("package"))
        rec["import_error"] = probe.get("package_error", "")
        got = probe.get("names") or {}
        rec["names_importable"] = sum(1 for v in got.values() if v)
        rec["names_ratio"] = round(rec["names_importable"] / len(names), 3) if names else 1.0
        rec["names_missing"] = sorted(k for k, v in got.items() if not v)[:50]
        try:
            c = run([py, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", f"--rootdir={cwd}", "-o", "addopts=",
                     "_unit_suites"])
            errs = re.findall(r"^ERROR (\S+)", c.stdout, re.M)
            rec["collect_errors"] = sorted(set(errs))
            rec["collects"] = c.returncode in (0, 5) and not errs
        except subprocess.TimeoutExpired:
            rec["collects"], rec["collect_errors"] = False, ["collection timed out"]
        alltext = "\n".join(docs.values())
        rec["cli_documented"] = bool(re.search(rf"python3?\s+-m\s+{re.escape(package)}\b", alltext))
        if rec["cli_documented"]:
            try:
                rec["cli_runs"] = run([py, "-m", package, "--help"]).returncode == 0
            except subprocess.TimeoutExpired:
                rec["cli_runs"] = False
        rec["submittable"] = int(rec["imports"] and rec["names_ratio"] >= NAMES_MIN and rec["collects"]
                                 and (rec.get("cli_runs", True)))
        rec["names_min"] = NAMES_MIN
        return rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
