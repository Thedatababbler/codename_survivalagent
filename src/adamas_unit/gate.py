"""The acceptance gate: frozen suites on a copy of the code, per-test results, three columns (DESIGN §4.4, USING §2.3).

``run_gate`` copies the code (no ``.git``, no caches) into a fresh gate directory, puts the frozen suites beside it
under ``_unit_suite__<unit_id>/`` (one level below the code root, as AdaMAS's ``spec_tests/``: a suite reading
``Path(__file__).parents[1] / "pyproject.toml"`` finds the repository's) and the authors' testkit under ``_unit_harness/``, and runs the task venv's pytest
there under the sandbox's workspace policy (``run_confined``: the gate copy writable, nothing else of the run
readable). The agent's working directory is never touched, so the gate leaves no cache in it.

Node ids are reported relative to the suite root (``<unit_id>/test_x.py::test_a``) and stay stable across rounds
because the suites are frozen. A test with no result when the gate stops (timeout, crash) counts as failed.

``columns`` splits a gate's failures by the per-test baseline: the outcome of each test at the most recent unit commit
that ran it (a unit's commit records every suite its gate ran, so the baseline of an earlier suite's test is its
own unit's commit, or a later one when that later gate ran it again):

1. failures of the current unit's suite;
2. inherited: an earlier suite's test that was failing at the baseline (or never ran before);
3. regression: an earlier suite's test that passed at the baseline and fails now.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orchestra.sandbox.guard import require_sandbox, run_confined

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = Path(__file__).with_name("gate_plugin.py")
TESTKIT = ROOT / "src" / "orchestra" / "harness" / "testkit"
SUITES_DIR = "_unit_suites"          # legacy layout (_unit_suites/<unit_id>/), still kept out of the copy
SUITE_PREFIX = "_unit_suite__"


def suite_dir_name(unit_id: str) -> str:
    return f"{SUITE_PREFIX}{unit_id}"
OK = ("passed",)


@dataclass
class GateResult:
    results: dict[str, str] = field(default_factory=dict)       # node id -> passed / failed / error / skipped / missing
    seconds: float = 0.0
    timed_out: bool = False
    returncode: int | None = None
    output_tail: str = ""

    def failed(self, prefix: str = "") -> list[str]:
        return sorted(n for n, o in self.results.items() if o not in OK and o != "skipped" and n.startswith(prefix))

    def passed(self, prefix: str = "") -> list[str]:
        return sorted(n for n, o in self.results.items() if o in OK and n.startswith(prefix))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_gate(code_dir: Path, suites: dict[str, Path], *, venv: Path, work_dir: Path, timeout: float,
             deselect: dict[str, list[str]] | None = None, keep: bool = False) -> GateResult:
    """Run the frozen ``suites`` ({unit_id: frozen suite dir}) against a copy of ``code_dir``."""
    require_sandbox("acceptance gate")
    work_dir, code_dir, venv = Path(work_dir).resolve(), Path(code_dir).resolve(), Path(venv)
    work_dir.mkdir(parents=True, exist_ok=True)
    gate_dir = Path(tempfile.mkdtemp(prefix="gate-", dir=work_dir))
    try:
        shutil.copytree(code_dir, gate_dir / "code", symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc", SUITES_DIR,
                                                      f"{SUITE_PREFIX}*"))
        cwd = gate_dir / "code"
        for uid, src in suites.items():
            shutil.copytree(src, cwd / suite_dir_name(uid), ignore=shutil.ignore_patterns("__pycache__", "SUITE.json"))
        shutil.copy2(PLUGIN, cwd / "_unit_gate_plugin.py")
        harness = cwd / "_unit_harness"
        shutil.copytree(TESTKIT / "adamas_testkit", harness / "adamas_testkit", ignore=shutil.ignore_patterns("__pycache__"))
        res_file = cwd / "_unit_gate_results.json"
        # the configuration file is pinned inside the gate copy: otherwise pytest >= 8 takes the nearest ancestor's
        # (this project's pyproject.toml) and starts collecting there, which the sandbox refuses (2026-11-07: every
        # deepdiff gate, pytest 9.1, ran nothing)
        ini = cwd / "_unit_pytest.ini"
        ini.write_text("[pytest]\n", encoding="utf-8")
        args = [str(Path(venv) / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "_unit_gate_plugin",
                "-c", str(ini), f"--rootdir={cwd}", "-o", "addopts=", "--continue-on-collection-errors"]
        for uid, nids in (deselect or {}).items():
            for n in nids:
                args.append(f"--deselect={SUITE_PREFIX}{n}")
        args += [suite_dir_name(u) for u in suites]
        env = {"PATH": f"{venv}/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": f"{cwd}{os.pathsep}{harness}",
               "UNIT_GATE_RESULTS": str(res_file), "PYTHONDONTWRITEBYTECODE": "1"}
        t0 = time.time()
        out = GateResult()
        try:
            p = run_confined(args, cwd=cwd, env=env, role="gate", capture_output=True, text=True, timeout=timeout)
            out.returncode, tail = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired as e:
            out.timed_out = True
            tail = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        out.seconds = round(time.time() - t0, 2)
        out.output_tail = tail[-6000:]
        raw = json.loads(res_file.read_text(encoding="utf-8")) if res_file.is_file() else {}
        out.results = {k.removeprefix(SUITE_PREFIX): v for k, v in raw.items()}
        # a suite of which nothing ran (pytest refused to start, the run hung before it) must not read as "no failures"
        for uid in suites:
            if not any(n.split("/", 1)[0] == uid for n in out.results):
                out.results[f"{uid}/::<nothing ran>"] = "error"
        return out
    finally:
        if not keep:
            shutil.rmtree(gate_dir, ignore_errors=True)


def case_ids(suite_dir: Path, unit_id: str) -> list[str]:
    """The suite's cases read from its source (``<unit_id>/test_x.py::test_a``, ``...::TestK::test_b``): known even
    when the suite cannot be collected yet (a top-level import of the package not written). A parametrised case is
    one id; its runs report ``id[param]``."""
    import ast

    out = []
    root = Path(suite_dir)
    for f in sorted(root.rglob("test_*.py")) + sorted(root.rglob("*_test.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = f"{unit_id}/{f.relative_to(root).as_posix()}"
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                out.append(f"{rel}::{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                out += [f"{rel}::{node.name}::{m.name}" for m in node.body
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")]
    return sorted(set(out))


def _runs(results: dict[str, str], case: str) -> list[str]:
    return [o for n, o in results.items() if n == case or (n.startswith(case + "[") and n.endswith("]"))]


def outcome(results: dict[str, str], case: str) -> str:
    """A case's outcome: its own result, or over its parametrised runs (passed only when every run passed)."""
    runs = _runs(results, case)
    if not runs:
        return "missing"
    if all(o in OK for o in runs):
        return "passed"
    return next(o for o in runs if o not in OK)


def drop(result: GateResult, cases: list[str] | set[str]) -> GateResult:
    """Remove the results of cases no gate counts (vacuous, soft) and of their parametrised runs; exact, unlike
    pytest's prefix ``--deselect``."""
    cs = set(cases)
    if cs:
        result.results = {n: o for n, o in result.results.items() if n.split("[")[0] not in cs}
    return result


def complete(result: GateResult, known: list[str] | dict[str, str]) -> GateResult:
    """Tests known from earlier gates or authoring that this gate did not report (killed on timeout, file collapsed)
    count as failed."""
    for n in known:
        if not _runs(result.results, n):
            result.results[n] = "missing"
    return result


@dataclass
class Columns:
    new: list[str]            # 1: the current unit's suite
    inherited: list[str]      # 2: earlier suites, already failing at their unit's commit
    regression: list[str]     # 3: earlier suites, passing at their unit's commit, failing now

    @property
    def passes(self) -> bool:
        """The unit passes its gate when columns 1 and 3 are empty."""
        return not self.new and not self.regression

    def counts(self) -> dict[str, int]:
        return {"new": len(self.new), "inherited": len(self.inherited), "regression": len(self.regression)}

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "counts": self.counts(), "passes": self.passes}


def columns(result: GateResult, unit_id: str, baseline: dict[str, str]) -> Columns:
    """Split the failures of ``result``. ``baseline``: {node id: outcome at the most recent commit that ran it}."""
    new, inh, reg = [], [], []
    for n in result.failed():
        uid = n.split("/", 1)[0]
        if uid == unit_id:
            new.append(n)
            continue
        base = baseline.get(n)
        if base in OK:
            reg.append(n)
        else:
            inh.append(n)             # failing at commit, or not seen then (a test that never passed is not a regression)
    return Columns(new=new, inherited=inh, regression=reg)


def commit_baseline(baseline: dict[str, str], result: GateResult) -> dict[str, str]:
    """The baseline after a unit is committed with gate ``result``: every test the gate ran takes its new outcome."""
    return {**baseline, **result.results}
