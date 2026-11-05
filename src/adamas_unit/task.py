"""A task (repository) and its units (docs/DESIGN.md §3, §4.1).

A task is a dataset repository in CodeProjectEval's on-disk shape (``config.json`` naming the PRD, UML, architecture
design, dependency list, visible ``check_tests`` and held-out ``unit_tests``); its units are the milestones of its
frozen plan (``configs/datasets/*_plans/<task>.plan.json``, read only).

``build_workspace`` follows AdaMAS's ``codeprojecteval.dataset.build_agent_workspace`` (34276d69): the design
documents, the dependency manifest and the visible check tests, never the reference implementation or the held-out
suite. Nothing here reads a file the config names as reference or held-out.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
NL2 = {"dataset": Path("/root/codex-benchmarks/nl2repo/cpe_format"), "envs": Path("/root/codex-benchmarks/nl2repo/envs"),
       "plans": ROOT / "configs" / "datasets" / "nl2repo_hard_plans"}
CPE = {"dataset": Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset"),
       "envs": Path("/root/codex-benchmarks/cpe_envs"), "plans": ROOT / "configs" / "datasets" / "cpe_feature_plans"}


@dataclass(frozen=True)
class Unit:
    index: int
    unit_id: str
    title: str
    objective: str
    criteria: list[str] = field(default_factory=list)
    corner_cases: list[str] = field(default_factory=list)
    focus_paths: list[str] = field(default_factory=list)

    def brief(self) -> str:
        """The unit description every session of the unit gets (title, objective, acceptance items)."""
        lines = [f"Unit {self.index + 1}: {self.title}", "", self.objective]
        if self.focus_paths:
            lines += ["", "Files this unit is about: " + ", ".join(self.focus_paths)]
        if self.criteria:
            lines += ["", "Acceptance criteria:", *[f"- {c}" for c in self.criteria]]
        if self.corner_cases:
            lines += ["", "Corner cases:", *[f"- {c}" for c in self.corner_cases]]
        return "\n".join(lines)


@dataclass(frozen=True)
class Task:
    task_id: str
    repo_root: Path
    venv: Path
    plan_file: Path
    config: dict[str, Any]
    units: list[Unit]

    @property
    def package(self) -> str:
        """The package (or module) the documents describe: the last part of ``source_code``."""
        return Path(str(self.config.get("source_code") or self.task_id.removeprefix("nl2_"))).name.removesuffix(".py")

    @property
    def doc_files(self) -> list[str]:
        uml = self.config.get("UML") or []
        uml = [uml] if isinstance(uml, str) else list(uml)
        return [p for p in [self.config.get("PRD"), *uml, self.config.get("architecture_design"), "docs/directory_tree.txt"] if p]

    @property
    def check_tests(self) -> str:
        return str(self.config.get("check_tests") or "check_tests")


def roots(task_id: str) -> dict[str, Path]:
    return NL2 if task_id.startswith("nl2_") else CPE


def load_task(task_id: str) -> Task:
    r = roots(task_id)
    repo = r["dataset"] / task_id
    cfg = json.loads((repo / "config.json").read_text(encoding="utf-8"))
    plan_file = r["plans"] / f"{task_id}.plan.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    units = []
    for i, m in enumerate(plan["milestones"]):
        acc = m.get("acceptance") or {}
        units.append(Unit(index=i, unit_id=str(m["milestone_id"]), title=str(m.get("title") or m["milestone_id"]),
                          objective=str(m.get("objective") or ""), criteria=list(acc.get("criteria") or []),
                          corner_cases=list(acc.get("corner_cases") or []), focus_paths=list(m.get("focus_paths") or [])))
    venv = r["envs"] / task_id
    if not (venv / "bin" / "python").exists():
        raise FileNotFoundError(f"{task_id}: task venv missing at {venv}")
    return Task(task_id=task_id, repo_root=repo, venv=venv, plan_file=plan_file, config=cfg, units=units)


def build_workspace(task: Task, dest: Path) -> Path:
    """An empty repository with the design documents, the dependency list and the visible check tests."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    rel = [*task.doc_files, str(task.config.get("dependencies") or "requirements.txt"),
           *[str(p) for p in task.config.get("required_files") or []], "README.md", "README.rst", task.check_tests]
    for r in dict.fromkeys(rel):
        src = task.repo_root / r
        if src.is_dir():
            shutil.copytree(src, dest / r, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        elif src.is_file():
            (dest / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / r)
    (dest / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n", encoding="utf-8")
    git = ["git", "-c", "user.email=unit@adamas", "-c", "user.name=adamas-unit"]
    subprocess.run([*git, "init", "-q"], cwd=dest, check=True)
    subprocess.run([*git, "add", "-A"], cwd=dest, check=True)
    subprocess.run([*git, "commit", "-qm", "dataset inputs only"], cwd=dest, check=True)
    return dest


def docs_of(workspace: Path) -> dict[str, str]:
    """The design documents in a workspace (as AdaMAS's ``public_symbols.load_docs``)."""
    out = {}
    for p in sorted((Path(workspace) / "docs").glob("*")):
        if p.is_file() and p.suffix.lower() in (".md", ".txt", ".rst"):
            out[p.name] = p.read_text(encoding="utf-8", errors="replace")
    return out


def copy_code(src: Path, dest: Path) -> Path:
    """A code snapshot: the tree without caches (``.git`` kept, so a unit continues the history)."""
    if Path(dest).exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    return Path(dest)
