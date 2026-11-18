"""Tasks of v0.3: the dataset repositories of v0.2 with the stage plans frozen for v0.3
(``configs/datasets/v03_plans/``, read-only copies; ``PROVENANCE.json`` names each one's source and hash)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from adamas_unit import importcheck as IC
from adamas_unit.task import NL2, Task, Unit

ROOT = Path(__file__).resolve().parents[2]
PLANS = ROOT / "configs" / "datasets" / "v03_plans"
MANIFESTS = ROOT / "configs" / "datasets" / "v03_import_manifests"
EVOLUTION_TASKS = ("nl2_python-pathspec", "nl2_python-fsutil", "nl2_deepdiff")


def load_task(task_id: str) -> Task:
    prov = json.loads((PLANS / "PROVENANCE.json").read_text(encoding="utf-8"))["plans"]
    plan_file = PLANS / f"{task_id}.plan.json"
    raw = plan_file.read_bytes()
    if task_id not in prov or hashlib.sha256(raw).hexdigest() != prov[task_id]["sha256"]:
        raise RuntimeError(f"{task_id}: the stage plan is not the frozen one")
    repo = NL2["dataset"] / task_id
    cfg = json.loads((repo / "config.json").read_text(encoding="utf-8"))
    units = []
    for i, m in enumerate(json.loads(raw)["milestones"]):
        acc = m.get("acceptance") or {}
        units.append(Unit(index=i, unit_id=str(m["milestone_id"]), title=str(m.get("title") or m["milestone_id"]),
                          objective=str(m.get("objective") or ""), criteria=list(acc.get("criteria") or []),
                          corner_cases=list(acc.get("corner_cases") or []), focus_paths=list(m.get("focus_paths") or [])))
    venv = NL2["envs"] / task_id
    if not (venv / "bin" / "python").exists():
        raise FileNotFoundError(f"{task_id}: task venv missing at {venv}")
    return Task(task_id=task_id, repo_root=repo, venv=venv, plan_file=plan_file, config=cfg, units=units)


def import_check(task: Task, cfg: dict | None = None) -> IC.ImportCheck:
    """The closing import check of v0.2 on the task's frozen v0.3 manifest (documents only; built once by
    ``scripts/v03_build_manifests.py``)."""
    m = IC.load_manifest(task.task_id, MANIFESTS / task.task_id / "manifest.json")
    docs = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in IC.manifest_inputs(task)}
    if [str(p) for p in IC.manifest_inputs(task)] != m["inputs"]:
        raise RuntimeError(f"{task.task_id}: the documents changed since the manifest was frozen")
    return IC.ImportCheck(cfg or IC.load_config(), m, docs)


def forbidden_names(task_ids: tuple[str, ...] = EVOLUTION_TASKS) -> set[str]:
    """Names a skill document may not contain: the tasks, their packages, their modules and the documented names
    that are not ordinary words (an underscore or an inner capital)."""
    out: set[str] = set()
    for t in task_ids:
        out.add(t)
        out.add(t.removeprefix("nl2_"))
        f = MANIFESTS / t / "manifest.json"
        if not f.is_file():
            continue
        m = json.loads(f.read_text(encoding="utf-8"))
        out.add(m["package"])
        for e in m["names"]:
            if e.get("module"):
                out.add(e["module"])
            n = e["name"]
            if "_" in n.strip("_") or any(c.isupper() for c in n[1:]):
                out.add(n)
    return {x for x in out if len(x) >= 4}
