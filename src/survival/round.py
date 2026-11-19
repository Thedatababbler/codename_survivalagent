"""One v0.3 round: the executor builds the whole repository, stage by stage, calling sub-agents when it pays for
them (refactor §2, §3, §5).

Layout of a round directory (``outputs/rounds_v03/<round>/<task>/``; a round never reuses a directory):

    ws/                       the executor's repository (the sub-agents work in it too, one at a time)
    executor/                 the executor's session: commands.jsonl, trajectory.json
    subagents/NN_<kind>/      one directory per sub-agent call (same files)
    final/                    the code submitted (the memory file removed), after the closing import check
    ledger.json               every budget event
    round.json                the round's record: measures, resources, stages, sub-agent calls, planner actions
    sandbox_manifest.json     one entry per session, for the breach scanner

The arms (§5.2) differ in two things only: the skill set (``pi0`` and ``evolved``), and whether sub-agents exist
(``single`` has none and keeps working after the last stage until the budget is used). The readout, the context
window and its roll-back, the stage minimum, the closing import check with its one fix (own ledger, v0.2
``importcheck``) are the same code for every arm.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from adamas_unit import importcheck as IC
from adamas_unit import submit_check
from adamas_unit.sandbox_env import ConfinedEnvironment
from adamas_unit.task import Task, build_workspace, docs_of
from orchestra.sandbox import policy as SB
from orchestra.sandbox.guard import run_confined
from survival import memory as MEM
from survival.agents import ExecutorAgent, ManagedAgent, WriteToken
from survival.ledger import Limits, Quota, RoundLedger
from survival.planner import Planner, build_state
from survival.skills import SkillSet

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "configs" / "roles_v03"
ARMS = ("pi0", "evolved", "single")


@dataclass
class RoundConfig:
    limits: Limits
    context_window: int = 64_000
    max_rollbacks: int = 6
    keep_recent: int = 6
    executor_max_steps: int = 400
    subagent_max_steps: int = 80
    command_timeout: int = 300
    probe_every: int = 10
    check_timeout: float = 180.0
    # the closing import fix runs on v0.2's agent, with v0.2's settings: the same for every arm
    importfix_agent_config: dict[str, Any] = field(default_factory=lambda: {"margin_tokens": 60_000, "context_window": 200_000,
                                                                            "keep_recent": 6})

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "limits": asdict(self.limits)}


@dataclass
class Models:
    """``agent()`` makes a session's tool-calling model; ``complete`` is the planner's plain call (None: no
    judgment calls, the fixed fallback); ``summarize`` the compaction summariser."""
    agent: Callable[[], Any]
    complete: Callable[[list[dict]], tuple[str, int, int]] | None = None
    summarize: Callable[[list[dict]], tuple[str, int, int]] | None = None


class OwnedEnvironment(ConfinedEnvironment):
    """A confined environment that runs a command only while its session holds the write token. ``read_only``: the
    reviewer's policy -- the repository is readable and not writable (the agent policy with the repository moved
    from the writable to the readable paths; nothing is added)."""

    def __init__(self, *, token: WriteToken, owner: str, read_only: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.token, self.owner, self.read_only = token, owner, read_only
        if read_only:
            ws = str(self.config.workspace)
            gd = set(SB.git_dirs(Path(ws)))
            pol = self.policy
            moved = [w for w in pol.write if w == ws or w in gd]
            self.policy = replace(pol, read=[*pol.read, *moved], write=[w for w in pol.write if w not in moved])
            self.policy_path = SB.write_policy(self.policy, self.private / "policy.json")

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        self.token.check(self.owner)
        return super().execute(action, cwd, timeout=timeout)


def role_text(name: str) -> str:
    return (ROLES / f"{name}.md").read_text(encoding="utf-8").strip()


def _strip_final(final: Path) -> None:
    (final / MEM.MEMORY_FILE).unlink(missing_ok=True)


class Round:
    review_read_only = True              # the reviewer's read-only policy (off only in the positive control, to
                                         # show that the second line of defence works on its own)

    def __init__(self, task: Task, run_dir: Path, cfg: RoundConfig, models: Models, skills: SkillSet, *,
                 arm: str = "pi0", importcheck: IC.ImportCheck | None = None, skills_id: str = "pi0"):
        if arm not in ARMS:
            raise ValueError(arm)
        # absolute: a relative path in a sandbox policy matches nothing (smoke forced-1, 2026-11-18: the agents could
        # not open their own repository)
        self.task, self.run_dir, self.cfg, self.models, self.skills, self.arm = task, Path(run_dir).resolve(), cfg, models, skills, arm
        self.ic, self.skills_id = importcheck, skills_id
        if self.run_dir.exists():
            raise FileExistsError(f"{self.run_dir}: a round never reuses a directory")
        self.run_dir.mkdir(parents=True)
        self.ws = self.run_dir / "ws"
        self.ledger = RoundLedger(cfg.limits, len(task.units), path=self.run_dir / "ledger.json")
        self.planner = Planner(skills, self.ledger, complete=models.complete)
        self.token = WriteToken()
        self.sub_records: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []
        self.reviewer_writes = 0
        self.executor: ExecutorAgent | None = None

    # ------------------------------------------------------------------ sessions

    def _env(self, role: str, owner: str, session_dir: Path, *, read_only: bool = False) -> OwnedEnvironment:
        session_dir.mkdir(parents=True, exist_ok=True)
        capture = session_dir / "commands.jsonl"
        env = OwnedEnvironment(token=self.token, owner=owner, read_only=read_only, workspace=str(self.ws), role=role,
                               capture=str(capture), venv=str(self.task.venv), timeout=self.cfg.command_timeout)
        SB.write_manifest(self.run_dir, {"kind": "agent", "role": role, "request_id": owner, "policy": env.policy.to_dict(),
                                         "capture": str(capture), "trajectory": str(session_dir / "trajectory.json"),
                                         "workspace": str(self.ws)})
        return env

    def _agent_kwargs(self, max_steps: int) -> dict[str, Any]:
        return {"system_template": "{{system_text}}", "instance_template": "{{task}}", "context_window": self.cfg.context_window,
                "max_rollbacks": self.cfg.max_rollbacks, "keep_recent": self.cfg.keep_recent, "max_steps": max_steps}

    def _sub_state(self, agent: ManagedAgent) -> dict[str, Any]:
        ex = self.executor
        return build_state(self.ledger, context_share=agent.window.share(agent.messages),
                           idle_steps=agent.tracker.steps_since_code_change,
                           missing_delta=ex.signals["missing_delta"] if ex else 0,
                           tests_delta=ex.signals["tests_delta"] if ex else 0, all_done=bool(ex and ex.all_done))

    def spawn(self, kind: str, task_text: str, quota: Quota) -> dict[str, Any]:
        """Run one sub-agent on the executor's repository (the executor is paused inside its own command)."""
        n = len(self.sub_records)
        sdir = self.run_dir / "subagents" / f"{n:02d}_{kind}"
        read_only = kind == "review" and self.review_read_only
        guard = kind == "review"
        before_code = MEM.code_hash(self.ws)
        before_mem = MEM.memory_hash(self.ws)
        snap = None
        if guard:                                       # second line of defence: restore if anything changed
            snap = sdir / "pre_ws"
            shutil.copytree(self.ws, snap, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        env = self._env(kind, quota.quota_id, sdir, read_only=read_only)
        stage = self.task.units[quota.stage]
        instance = f"{task_text}\n\n## The stage this belongs to\n\n{stage.brief()}"
        if kind == "worker":
            instance += f"\n\n## The other agent's memory file ({MEM.MEMORY_FILE})\n\n{MEM.read_memory(self.ws)}"
        instance += (f"\n\nYour token allowance for this job is limited; in the resource line, `grant tokens` (shown in place of `stage tokens`) is the "
                     "share of your own allowance already used. Finish before it runs out.")
        agent = ManagedAgent(self.models.agent(), env, ledger=self.ledger, name=quota.quota_id, ws=self.ws, token=self.token,
                             quota_id=quota.quota_id, summarizer=self.models.summarize, state_fn=self._sub_state,
                             **self._agent_kwargs(self.cfg.subagent_max_steps))
        t0 = time.time()
        try:
            out = agent.run(instance, system_text=role_text(kind) + "\n\n" + role_text("common"))
        finally:
            env.close()
            agent.save(sdir / "trajectory.json")
        rec: dict[str, Any] = {"n": n, "exit_status": out.get("exit_status"), "result": str(out.get("submission") or ""),
                               "seconds": round(time.time() - t0, 1), "session": agent.record(),
                               "wrote_memory": MEM.memory_hash(self.ws) != before_mem,
                               "code_changed": MEM.code_hash(self.ws) != before_code}
        if guard:
            changed = rec["code_changed"] or rec["wrote_memory"]
            if changed:                                 # the policy should have refused it: undo and flag
                self.reviewer_writes += 1
                for p in list(self.ws.iterdir()):
                    if p.name != ".git":
                        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
                shutil.copytree(snap, self.ws, symlinks=True, dirs_exist_ok=True)
                self.ledger.note("reviewer_wrote", quota=quota.quota_id)
                rec["reverted"] = True
            shutil.rmtree(snap, ignore_errors=True)
        (sdir / "session.json").write_text(json.dumps({**rec, "kind": kind, "quota": quota.tokens, "used": quota.used},
                                                      indent=1, default=str), encoding="utf-8")
        self.sub_records.append({"n": n, "kind": kind})
        self.sessions.append({"name": quota.quota_id, "kind": kind, **agent.record()})
        return rec

    # ------------------------------------------------------------------ harness checks (confined, not charged)

    def _copy(self) -> tuple[Path, Path]:
        base = Path(tempfile.mkdtemp(prefix="check-", dir=self.run_dir / "checks"))
        cwd = base / "code"
        shutil.copytree(self.ws, cwd, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"))
        (cwd / "_v03_pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        return base, cwd

    def _pytest(self, cwd: Path, extra: list[str], timeout: float) -> subprocess.CompletedProcess | None:
        py = str(self.task.venv / "bin" / "python")
        env = {"PATH": f"{self.task.venv}/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": str(cwd),
               "PYTHONDONTWRITEBYTECODE": "1"}
        ignore = [f"--ignore={self.task.check_tests}"] if (cwd / self.task.check_tests).exists() else []
        try:
            return run_confined([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(cwd / "_v03_pytest.ini"),
                                 f"--rootdir={cwd}", "-o", "addopts=", *ignore, *extra], cwd=cwd, env=env, role="stage_check",
                                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def min_check(self) -> list[str]:
        """The stage's minimum condition (user decision 2026-11-18): the package imports; the executor's own tests
        can be collected (they need not pass). Documented names are not part of it."""
        (self.run_dir / "checks").mkdir(exist_ok=True)
        base, cwd = self._copy()
        py = str(self.task.venv / "bin" / "python")
        env = {"PATH": f"{self.task.venv}/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"}
        out: list[str] = []
        try:
            try:
                p = run_confined([py, "-c", f"import {self.task.package}"], cwd=cwd, env=env, role="stage_check",
                                 capture_output=True, text=True, timeout=self.cfg.check_timeout)
                if p.returncode != 0:
                    tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["import failed"]
                    out.append(f"`import {self.task.package}` fails: {tail[0][:300]}")
            except subprocess.TimeoutExpired:
                out.append(f"`import {self.task.package}` timed out")
            c = self._pytest(cwd, ["--collect-only"], self.cfg.check_timeout)
            if c is None:
                out.append("collecting your tests timed out")
            else:
                errs = sorted(set(re.findall(r"^ERROR (\S+)", c.stdout or "", re.M)))
                if c.returncode not in (0, 5) or errs:
                    out.append("your tests cannot be collected" + (": " + ", ".join(e.replace(str(cwd) + "/", "") for e in errs[:5]) if errs else ""))
        finally:
            shutil.rmtree(base, ignore_errors=True)
        return out

    def probe(self) -> dict[str, Any]:
        """Progress figures for the planner state: documented names missing so far (the import check, recorded
        only) and the executor's own tests passing."""
        (self.run_dir / "checks").mkdir(exist_ok=True)
        got: dict[str, Any] = {"missing": None, "tests_passed": None}
        if self.ic is not None:
            scope = IC.scope_of(self.ic.manifest, self.ledger.current, self.ic.cfg["importcheck"].get("unattributed", "top_level"))
            try:
                r = IC.check(self.ws, scope=scope, package=self.ic.manifest["package"], venv=self.task.venv,
                             work_dir=self.run_dir / "checks" / "importcheck")
                got["missing"] = len(r.get("names_missing") or [])
            except Exception:  # noqa: BLE001 - a failed measurement is no measurement
                pass
        base, cwd = self._copy()
        try:
            p = self._pytest(cwd, [], self.cfg.check_timeout)
            if p is not None:
                m = re.search(r"(\d+) passed", p.stdout or "")
                got["tests_passed"] = int(m.group(1)) if m else 0
        finally:
            shutil.rmtree(base, ignore_errors=True)
        return got

    # ------------------------------------------------------------------ the round

    def system_text(self) -> str:
        sub = role_text("executor_single" if self.arm == "single" else "executor_subagents")
        return (role_text("executor").replace("{{subagent_text}}", sub).replace("{{readout_guidance}}", self.skills.readout_guidance())
                + "\n\n" + role_text("common"))

    def instance_text(self) -> str:
        n = len(self.task.units)
        plan = "\n".join(f"{i + 1}. {u.title}" for i, u in enumerate(self.task.units))
        lim = self.cfg.limits
        return (f"Build the Python package `{self.task.package}` described in `docs/`.\n\nThe stages, in order:\n{plan}\n\n"
                f"Limits of this run: {lim.compressions} context compactions"
                + ("" if self.arm == "single" else f"; sub-agent calls: {lim.reviews} reviews, {lim.repairs} repairs, "
                   f"{lim.workers} workers, {lim.subagents} in all")
                + f".\n\nYou are in stage 1/{n}:\n\n{self.task.units[0].brief()}")

    def run(self) -> dict[str, Any]:
        t0 = time.time()
        build_workspace(self.task, self.ws)
        with (self.ws / ".gitignore").open("a", encoding="utf-8") as fh:
            fh.write(f"{MEM.MEMORY_FILE}\n")
        MEM.init_memory(self.ws)
        self.ledger.open_stage(0)
        sdir = self.run_dir / "executor"
        env = self._env("executor", "executor", sdir)
        self.token.take("executor")
        agent = ExecutorAgent(self.models.agent(), env, ledger=self.ledger, name="executor", ws=self.ws, token=self.token,
                              summarizer=self.models.summarize, planner=self.planner, stages=list(self.task.units),
                              arm=self.arm, spawn=self.spawn, min_check=self.min_check, probe=self.probe,
                              probe_every=self.cfg.probe_every, **self._agent_kwargs(self.cfg.executor_max_steps))
        self.executor = agent
        try:
            out = agent.run(self.instance_text(), system_text=self.system_text())
        finally:
            env.close()
            agent.save(sdir / "trajectory.json")
        if not self.ledger.stage().closed:
            agent._close_stage(declared=False, problems=[], why=f"run ended ({out.get('exit_status')})")
        self.sessions.insert(0, {"kind": "executor", **agent.record()})
        final = self.run_dir / "final"
        shutil.copytree(self.ws, final, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
        _strip_final(final)
        final_ic = None
        if self.ic is not None:                         # the same closing check and one fix as every arm of v0.2
            th = time.time()
            final_ic = self.ic.run(where="final", upto_unit=None, ws=final, docs=self.ic.docs, venv=self.task.venv,
                                   work_dir=self.run_dir, model_factory=self.models.agent,
                                   agent_config=self.cfg.importfix_agent_config, command_timeout=self.cfg.command_timeout,
                                   run_dir=self.run_dir)
            self.ledger.side_charge("harness", seconds=time.time() - th, what="importcheck final")
        sub = submit_check.check(final, package=self.task.package, docs=docs_of(final), suites={}, venv=self.task.venv,
                                 work_dir=self.run_dir / "submit")
        # v0.3 has no frozen suites: submittable = the package imports, its documented names import, its CLI runs
        submittable = int(sub["imports"] and sub["names_ratio"] >= sub["names_min"] and sub.get("cli_runs", True))
        ex = agent.record()
        peaks = [s["context"]["peak"] for s in self.sessions if s["context"]["peak"]]
        resources = {"tokens": self.ledger.used, "repairs": self.ledger.calls["repair"], "compressions": self.ledger.compressions,
                     "peak_context": round(sum(peaks) / len(peaks), 1) if peaks else 0.0}
        counts = {"reviews": self.ledger.calls["review"], "workers": self.ledger.calls["worker"],
                  "subagents": self.ledger.subagents_used(), "handoffs": self.ledger.handoffs,
                  "rollbacks": self.ledger.rollbacks, "topups": sum(1 for d in self.planner.decisions if d["granted"] > 0),
                  "stages_declared": sum(1 for s in ex["stages"] if s["declared_done"]),
                  "stages_below_minimum": sum(1 for s in ex["stages"] if s["below_minimum"]),
                  "stages_closed_by_program": sum(1 for s in ex["stages"] if not s["declared_done"]),
                  "reviewer_writes_reverted": self.reviewer_writes}
        rec = {"task": self.task.task_id, "arm": self.arm, "skills": self.skills_id, "config": self.cfg.to_dict(),
               "exit_status": out.get("exit_status"),
               "measures": {"submittable": submittable}, "submit_check": sub, "resources": resources, "counts": counts,
               "stages": ex["stages"], "subagents": ex["subagents"], "requests": ex["requests"],
               "planner": {"fired": self.planner.fired, "decisions": self.planner.decisions},
               "sessions": self.sessions, "ledger": self.ledger.snapshot(), "wall_seconds": round(time.time() - t0, 1)}
        if self.ic is not None:
            rec["importcheck"] = {**self.ic.summary(), "final": final_ic}
        (self.run_dir / "round.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        return rec
