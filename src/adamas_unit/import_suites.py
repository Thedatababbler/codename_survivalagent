"""Frozen suites taken over from an AdaMAS run instead of authored here (user decision 2026-11-07, deepdiff).

The AdaMAS run must have used the same frozen milestone plan as this task (every milestone identical: id, title,
objective, dependencies, focus paths, acceptance items); otherwise nothing is imported. Its gate suites are the
custody copies ``<run>/<task>/harness/<unit>.spec_tests/``.

Each candidate run's suites are audited with the fixed audits (``suite_audit``: citations, interface, fake objects,
shims, ...; ``fixture_check``: fixture setup) on the docs-only workspace; the run with fewer violating cases is
chosen. No reference implementation and no held-out file is read or used for the choice. The violating cases are not
fixed (no author is called): they are marked *soft* -- deselected from every gate and probe like vacuous cases, so
they count nowhere and never cause a repair. A violation outside any case (module level) makes every case of that
file soft.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from adamas_unit.authoring import SPEC_DIR, FrozenSuiteError, freeze
from adamas_unit.gate import TESTKIT, case_ids, outcome, run_gate
from adamas_unit.task import Task, build_workspace, docs_of
from orchestra.codeprojecteval import fixture_check
from orchestra.codeprojecteval.suite_audit import audit_suite

PLAN_KEYS = ("milestone_id", "title", "objective", "depends_on", "focus_paths", "acceptance", "gate_level")


def plan_matches(task: Task, run_task_dir: Path) -> list[str]:
    """Differences between the task's frozen plan and the plan the AdaMAS run used ([] = identical)."""
    ours = json.loads(Path(task.plan_file).read_text(encoding="utf-8"))["milestones"]
    theirs = json.loads((Path(run_task_dir) / "milestone_plan_draft.json").read_text(encoding="utf-8"))["milestones"]
    if len(ours) != len(theirs):
        return [f"{len(ours)} milestones here, {len(theirs)} in the run"]
    out = []
    for a, b in zip(ours, theirs):
        out += [f"{a.get('milestone_id')}: {k}" for k in PLAN_KEYS if a.get(k) != b.get(k)]
    if [u.unit_id for u in task.units] != [m["milestone_id"] for m in theirs]:
        out.append("unit order")
    return out


def _case_of(node: str) -> tuple[str, str]:
    """'uid/test_x.py::Class::test_y[p]' -> ('test_x.py', 'test_y')"""
    path, _, rest = node.partition("::")
    return Path(path).name, rest.split("::")[-1].split("[")[0]


def audit_unit(task: Task, k: int, suite_src: Path, work: Path, *, gate_timeout: float = 900) -> dict[str, Any]:
    """Audit one imported suite on the docs-only workspace; returns its record (nodes, vacuous, soft, violations)."""
    unit = task.units[k]
    ws = build_workspace(task, work / "ws")
    shutil.copytree(suite_src, ws / SPEC_DIR, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    docs = docs_of(ws)
    a = audit_suite(ws / SPEC_DIR, docs, packages=[task.package], interface=True)
    fx, _ = fixture_check.check(ws, python=Path(task.venv) / "bin" / "python", packages=[task.package],
                                testkit=TESTKIT, suite_dir=SPEC_DIR)
    viol = list(a.violations) + list(fx)
    empty = build_workspace(task, work / "empty")
    r = run_gate(empty, {unit.unit_id: ws / SPEC_DIR}, venv=task.venv, work_dir=work / "gate", timeout=gate_timeout)
    nodes = case_ids(ws / SPEC_DIR, unit.unit_id)
    soft = set()
    for v in viol:
        f = Path(v.file).name
        for n in nodes:
            nf, nc = _case_of(n)
            if nf == f and (not v.case or nc == v.case.split("::")[-1].split("[")[0]):
                soft.add(n)
    by_rule: dict[str, int] = {}
    for v in viol:
        by_rule[v.rule] = by_rule.get(v.rule, 0) + 1
    shutil.rmtree(empty, ignore_errors=True)
    return {"unit": unit.unit_id, "index": k, "nodes": nodes, "vacuous": [n for n in nodes if outcome(r.results, n) == "passed"], "soft": sorted(soft),
            "violations": by_rule, "violation_items": [v.to_dict() for v in viol],
            "module_level": sum(1 for v in viol if not v.case),
            "empty_gate": {"failed": len(r.failed()), "passed": len(r.passed()), "seconds": r.seconds,
                           "timed_out": r.timed_out},
            "_ws": str(ws)}


def audit_run(task: Task, run_task_dir: Path, work: Path) -> dict[str, Any]:
    diff = plan_matches(task, run_task_dir)
    if diff:
        raise FrozenSuiteError(f"{run_task_dir}: the run's milestone plan differs: {diff}")
    units = {}
    for k, u in enumerate(task.units):
        src = Path(run_task_dir) / "harness" / f"{u.unit_id}.spec_tests"
        if not src.is_dir():
            raise FrozenSuiteError(f"{src}: no frozen gate suite for {u.unit_id}")
        units[u.unit_id] = audit_unit(task, k, src, work / f"{k:02d}_{u.unit_id}")
    return {"run": str(run_task_dir), "units": units,
            "soft_cases": sum(len(r["soft"]) for r in units.values()),
            "violations": sum(sum(r["violations"].values()) for r in units.values()),
            "cases": sum(len(r["nodes"]) for r in units.values())}


def import_set(task: Task, runs: list[Path], suite_set: Path) -> dict[str, Any]:
    """Audit every candidate run, freeze the one with fewer violating cases (ties: fewer violations, then the
    first given) as ``suite_set``."""
    suite_set = Path(suite_set)
    if suite_set.exists():
        raise FrozenSuiteError(f"{suite_set} exists: a frozen suite is never overwritten")
    work = suite_set.parent / f"{suite_set.name}.import"
    audits = [audit_run(task, Path(r), work / f"run{i}") for i, r in enumerate(runs)]
    best = min(range(len(audits)), key=lambda i: (audits[i]["soft_cases"], audits[i]["violations"], i))
    chosen = audits[best]
    from adamas_unit.authoring import tree_hash
    manifest: dict[str, Any] = {"task": task.task_id, "plan": str(task.plan_file), "source": "adamas_run",
                                "source_run": chosen["run"], "units": {},
                                "candidates": [{"run": a["run"], "cases": a["cases"], "soft_cases": a["soft_cases"],
                                                "violations": a["violations"]} for a in audits]}
    for k, u in enumerate(task.units):
        rec = dict(chosen["units"][u.unit_id])
        src = Path(chosen["run"]) / "harness" / f"{u.unit_id}.spec_tests"
        rec["source_suite"], rec["source_tree_sha256"] = str(src), tree_hash(src)
        ws = Path(rec.pop("_ws"))
        h = freeze(ws / SPEC_DIR, suite_set / u.unit_id, rec)
        manifest["units"][u.unit_id] = {"index": k, "tree_sha256": h, "nodes": len(rec["nodes"]),
                                        "vacuous": len(rec["vacuous"]), "soft": len(rec["soft"]),
                                        "source_tree_sha256": rec["source_tree_sha256"]}
    (suite_set / "MANIFEST.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    (work / "audits.json").write_text(json.dumps([{k: v for k, v in a.items()} for a in audits], indent=1, default=str),
                                      encoding="utf-8")
    return manifest
