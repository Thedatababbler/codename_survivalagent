"""The authoring phase: one frozen suite per unit, written once before all rounds (DESIGN §4.2, D1; USING §4 item 7).

For every unit, in its own docs-only workspace (``build_workspace``: documents, dependency list, visible check tests;
no code exists yet, so nothing can leak):

1. the test author (AdaMAS's fixed rules ``configs/roles/test_author/core_rules.md``, memory and evolution off)
   writes ``spec_tests/``;
2. the mechanical audit (AdaMAS ``suite_audit.audit_suite``: citations, private names, error paths, shims, imports,
   fake objects, interface) -- violations go back to the author in a fresh session, at most ``fix_rounds`` times;
3. the vacuity check: the gate runs the suite on the docs-only workspace; a test that passes there is vacuous and is
   deselected for good; every node id seen is recorded (a later gate that does not report one counts it failed);
4. freeze: ``spec_tests/`` is copied to ``<suite_set>/<unit_id>/`` with ``SUITE.json`` (files' sha256, node ids,
   vacuous ids, audit counts); ``MANIFEST.json`` holds every unit's tree hash. ``verify`` recomputes them; a round
   refuses to start on any mismatch.

Not ported from AdaMAS (recorded in the manifest): the behaviour inventory (it was off for NL2Repo there too), the
soft-case split and the fixture-setup check.

Authoring has its own ledger (its tokens are the "authoring" cost of DESIGN §13.1, outside every round's B).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adamas_unit.gate import run_gate
from adamas_unit.ledger import Budget, Ledger
from adamas_unit.sessions import ROLES, run_session
from adamas_unit.task import Task, build_workspace, docs_of
from orchestra.codeprojecteval.suite_audit import audit_suite

SPEC_DIR = "spec_tests"
NOT_PORTED = ["behaviour inventory", "soft-case split", "fixture-setup check"]


class FrozenSuiteError(RuntimeError):
    """A frozen suite is missing or its hash does not match (a round must not start)."""


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(Path(root).rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts and f.name != "SUITE.json":
            h.update(str(f.relative_to(root)).encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def author_prompt() -> str:
    core = (ROLES / "test_author" / "core_rules.md").read_text(encoding="utf-8").strip()
    return core + "\n\n" + (ROLES / "common.md").read_text(encoding="utf-8").strip()


def _violations_text(audit) -> str:
    lines = [f"- [{v.rule}] {v.file}{'::' + v.case if v.case else ''} (line {v.line}): {v.detail}" for v in audit.violations]
    return "\n".join(lines[:200])


def author_unit(task: Task, unit_index: int, *, out_dir: Path, ledger: Ledger, models: Callable[[], Any],
                fix_rounds: int = 2,
                gate_timeout: float = 900, agent_config: dict[str, Any] | None = None,
                summarizer: Callable | None = None) -> dict[str, Any]:
    """Author, audit, fix and check one unit's suite; returns its record (the suite is left in the workspace)."""
    unit = task.units[unit_index]
    udir = Path(out_dir) / f"{unit_index:02d}_{unit.unit_id}"
    ws = build_workspace(task, udir / "ws")
    docs = docs_of(ws)
    rec: dict[str, Any] = {"unit": unit.unit_id, "index": unit_index, "sessions": [], "audits": []}
    instance = (f"{unit.brief()}\n\nWrite this unit's test suite under `{SPEC_DIR}/` now. The package is "
                f"`{task.package}`; its code does not exist yet.")
    ledger.open_unit(unit_index)
    for rnd in range(fix_rounds + 1):
        lease = ledger.lease(unit_index, "test_author")
        s = run_session(role="test_author", system=author_prompt(), instance=instance, workspace=ws, venv=task.venv,
                        session_dir=udir / f"session{rnd}", ledger=ledger, lease=lease,
                        model=models(), manifest_dir=out_dir,
                        summarizer=summarizer, agent_config=agent_config)
        rec["sessions"].append(s)
        suite = ws / SPEC_DIR
        if not suite.is_dir() or not any(suite.rglob("test_*.py")):
            rec["audits"].append({"round": rnd, "error": "no spec_tests/test_*.py written"})
            instance = f"{unit.brief()}\n\nNo test file was found under `{SPEC_DIR}/`. Write the suite there."
            continue
        a = audit_suite(suite, docs, packages=[task.package], interface=True)
        rec["audits"].append({"round": rnd, "cases": a.cases, "cited": a.cited, "violations": a.by_rule()})
        if a.ok:
            break
        instance = ((ROLES / "author_fix.md").read_text(encoding="utf-8").strip() + "\n\n" + _violations_text(a)
                    + "\n\n" + unit.brief())
    ledger.close_unit(unit_index)
    suite = ws / SPEC_DIR
    if not suite.is_dir() or not any(suite.rglob("test_*.py")):
        raise FrozenSuiteError(f"{unit.unit_id}: the author wrote no suite")
    # vacuity: run on the docs-only repository (a fresh copy, without the suite itself)
    empty = build_workspace(task, udir / "empty")
    r = run_gate(empty, {unit.unit_id: suite}, venv=task.venv, work_dir=udir / "gate", timeout=gate_timeout)
    rec["nodes"] = sorted(n for n in r.results if not n.endswith("::<nothing ran>"))
    rec["vacuous"] = r.passed()
    rec["empty_gate"] = {"failed": len(r.failed()), "passed": len(r.passed()), "seconds": r.seconds, "timed_out": r.timed_out}
    shutil.rmtree(empty, ignore_errors=True)
    return rec


def freeze(suite_src: Path, dest: Path, rec: dict[str, Any]) -> str:
    if dest.exists():
        raise FrozenSuiteError(f"{dest} exists: a frozen suite is never overwritten")
    shutil.copytree(suite_src, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    for f in dest.rglob("*"):
        if f.is_file():
            f.chmod(0o444)
    h = tree_hash(dest)
    files = {str(f.relative_to(dest)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(dest.rglob("*")) if f.is_file()}
    (dest / "SUITE.json").write_text(json.dumps({**rec, "tree_sha256": h, "files": files}, indent=1, default=str), encoding="utf-8")
    return h


def author_all(task: Task, suite_set: Path, *, model: Any = None, budget: Budget, fix_rounds: int = 2,
               agent_config: dict[str, Any] | None = None, summarizer: Callable | None = None,
               only: list[int] | None = None, models: Callable[[], Any] | None = None) -> dict[str, Any]:
    """The whole phase for one task: ``suite_set`` is the frozen set's directory (must not exist). ``models``: a
    factory making each session's model (default: ``model`` for every session)."""
    suite_set = Path(suite_set)
    if (suite_set / "MANIFEST.json").exists():
        raise FrozenSuiteError(f"{suite_set} is already frozen")
    # named after the task, as the breach scanner expects (its project names come from the directory name)
    work = suite_set.parent / f"{suite_set.name}.authoring" / task.task_id
    work.mkdir(parents=True, exist_ok=True)
    (work / "plan.yaml").write_text("focus_paths:\n" + "".join(f"  - {p}\n" for u in task.units for p in u.focus_paths),
                                    encoding="utf-8")
    ledger = Ledger(budget, len(task.units), path=work / "authoring_ledger.json", floor_share=0.0)
    manifest: dict[str, Any] = {"task": task.task_id, "plan": str(task.plan_file), "units": {}, "not_ported": NOT_PORTED}
    for i, _ in enumerate(task.units):
        if only is not None and i not in only:
            continue
        rec = author_unit(task, i, out_dir=work, ledger=ledger, models=models or (lambda: model), fix_rounds=fix_rounds,
                          agent_config=agent_config, summarizer=summarizer)
        uid = task.units[i].unit_id
        h = freeze(work / f"{i:02d}_{uid}" / "ws" / SPEC_DIR, suite_set / uid, rec)
        manifest["units"][uid] = {"index": i, "tree_sha256": h, "nodes": len(rec["nodes"]), "vacuous": len(rec["vacuous"])}
    manifest["authoring_tokens"] = ledger.used.tokens
    manifest["authoring_seconds"] = round(ledger.elapsed_agent_seconds(), 1)
    (suite_set / "MANIFEST.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def verify(suite_set: Path, task: Task) -> dict[str, dict[str, Any]]:
    """Every unit's frozen suite present and unchanged; returns {unit_id: its SUITE.json}. Raises otherwise."""
    suite_set = Path(suite_set)
    mf = suite_set / "MANIFEST.json"
    if not mf.is_file():
        raise FrozenSuiteError(f"{suite_set}: no MANIFEST.json (authoring not finished)")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    out = {}
    for u in task.units:
        ent = manifest["units"].get(u.unit_id)
        d = suite_set / u.unit_id
        if ent is None or not d.is_dir():
            raise FrozenSuiteError(f"{u.unit_id}: frozen suite missing")
        h = tree_hash(d)
        if h != ent["tree_sha256"]:
            raise FrozenSuiteError(f"{u.unit_id}: frozen suite changed ({h[:12]} != {ent['tree_sha256'][:12]})")
        meta = json.loads((d / "SUITE.json").read_text(encoding="utf-8"))
        if meta.get("tree_sha256") != h:
            raise FrozenSuiteError(f"{u.unit_id}: SUITE.json does not match the suite")
        out[u.unit_id] = meta
    return out
