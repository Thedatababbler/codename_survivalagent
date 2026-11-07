"""One round: all units of a task in order, one ledger, then the submittable check and the round's measures
(DESIGN §4, §5.5, §7, §8.3; USING §4 items 5, 12).

Run directory (``<rounds root>/<round_id>/<task>/``; named after the task, as the breach scanner expects):

    plan.yaml                      the units' focus paths (for the scanner's project names)
    ledger.json                    the round ledger (events + state)
    sandbox_manifest.json          one entry per agent session (policy, capture, trajectory, workspace)
    input/                         the starting repository (documents only)
    snapshots/<k>_<unit>/          the code at the start of unit k (unit replay, DESIGN §9.2)
    units/<k>_<unit>/ws/           the unit's working copy (build and repairs happen in place)
    units/<k>_<unit>/repair_evidence/   failures and suites for the current repair (read only to the repairer)
    units/<k>_<unit>/<session>/    commands.jsonl, trajectory.json, session.json
    units/<k>_<unit>/unit.json     the unit's record: gates (three columns), repairs, decisions, use
    final/                         the final code
    round.json                     the round's record and measures

Unit flow: build -> gate -> while column 1 or 3 is not empty and repairs are left: repair -> gate; when the unit's
own gate passes and column 2 is not empty, the planner decides whether a repair goes to the inherited failures.
Repairs used up and still failing: continue with the code as it is (continue mode). Round budget exhausted: no
more sessions; the remaining units only run their gates for the record.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from adamas_unit import submit_check
from adamas_unit.authoring import verify
from adamas_unit.gate import TESTKIT, Columns, GateResult, columns, commit_baseline, complete, drop, outcome, run_gate
from adamas_unit.ledger import Budget, Ledger
from adamas_unit.planner import BudgetPlanner
from adamas_unit.progress import Prober
from adamas_unit.sessions import role_prompt, run_session
from adamas_unit.task import Task, build_workspace, copy_code, docs_of


@dataclass
class RoundConfig:
    budget: Budget
    floor_share: float = 0.05
    management_cap_share: float = 0.02
    max_repairs_per_unit: int = 3
    probe_every: int = 0                   # progress probe every N model calls (0: off); N is set in E0
    gate_timeout: float = 1200
    probe_timeout: float = 300
    command_timeout: int = 300
    agent_config: dict[str, Any] = field(default_factory=lambda: {"margin_tokens": 60_000, "context_window": 200_000,
                                                                   "keep_recent": 6})

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "budget": asdict(self.budget)}


@dataclass
class Models:
    """What the round calls: ``agent()`` makes a session's tool-calling model; ``complete`` is the planner's plain
    call (None: the planner's fixed rule); ``summarize`` the compression summariser (None: no compression)."""
    agent: Callable[[], Any]
    complete: Callable[[list[dict]], tuple[str, int, int]] | None = None
    summarize: Callable[[list[dict]], tuple[str, int, int]] | None = None


def _evidence(dest: Path, cols: Columns, gate: GateResult, suites: dict[str, Path], target: str) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "suites").mkdir(parents=True)
    for uid, src in suites.items():
        shutil.copytree(src, dest / "suites" / uid, ignore=shutil.ignore_patterns("__pycache__", "SUITE.json"))
    shutil.copytree(TESTKIT / "adamas_testkit", dest / "harness" / "adamas_testkit", ignore=shutil.ignore_patterns("__pycache__"))
    focus = {"own": "Fix the failures of this unit's suite (new) and the regressions first.",
             "inherited": "This unit's own suite passes. Fix the inherited failures of earlier units' suites, "
                          "without breaking anything that passes now."}[target]
    lines = ["# Gate failures", "", focus, ""]
    for name, ids in (("new: this unit's suite", cols.new), ("regression: passed before, fails now", cols.regression),
                      ("inherited: already failing when its unit was committed", cols.inherited)):
        lines += [f"## {name} ({len(ids)})", *[f"- {i}" for i in ids[:300]], ""]
    lines += ["## pytest output (tail)", "```", gate.output_tail, "```"]
    (dest / "failures.md").write_text("\n".join(lines), encoding="utf-8")
    for f in dest.rglob("*"):
        if f.is_file():
            f.chmod(0o444)


def single_unit(task: Task) -> Task:
    """The single-session control arm (DESIGN §13 E0): the whole repository as one unit, every unit's brief merged."""
    from dataclasses import replace

    from adamas_unit.task import Unit

    whole = Unit(index=0, unit_id="whole", title="the whole repository",
                 objective="Build the whole repository described in the documents. Its parts, in order:\n\n"
                           + "\n\n".join(u.brief() for u in task.units),
                 focus_paths=[p for u in task.units for p in u.focus_paths])
    return replace(task, units=[whole])


class Round:
    def __init__(self, task: Task, suite_set: Path, run_dir: Path, cfg: RoundConfig, models: Models, *,
                 table: dict[str, Any] | None = None, rules: list[dict[str, Any]] | None = None, single: bool = False):
        """``single``: the single-session control: one unit, its gate all frozen suites, the same B (repairs too)."""
        self.suite_set, self.run_dir = Path(suite_set), Path(run_dir)
        if self.run_dir.exists():
            raise FileExistsError(f"{self.run_dir}: a round never reuses a directory")
        self.meta = verify(self.suite_set, task)            # refuses on a missing or changed frozen suite
        self.orig_units = list(task.units)
        self.single = single
        if single:
            task, table = single_unit(task), None
            cfg = RoundConfig(**{**cfg.__dict__, "max_repairs_per_unit": cfg.budget.repairs})
        self.task, self.cfg, self.models = task, cfg, models
        self.run_dir.mkdir(parents=True)
        self.table = table
        self.ledger = Ledger(cfg.budget, len(task.units), path=self.run_dir / "ledger.json", floor_share=cfg.floor_share,
                             management_cap_share=cfg.management_cap_share,
                             max_repairs_per_unit=cfg.max_repairs_per_unit)
        self.prober: Prober | None = None
        self.planner = BudgetPlanner(self.ledger, complete=models.complete, table=table, rules=rules,
                                     progress_of=lambda lid: self.prober.get(lid) if self.prober else None)
        self.baseline: dict[str, str] = {}
        self.units: list[dict[str, Any]] = []
        focus = [p for u in task.units for p in u.focus_paths]
        (self.run_dir / "plan.yaml").write_text("focus_paths:\n" + "".join(f"  - {p}\n" for p in focus), encoding="utf-8")

    # ------------------------------------------------------------------ pieces

    def _orig_upto(self, k: int) -> list:
        """The frozen suites' units up to unit k (single arm: all of them)."""
        return self.orig_units if self.single else self.task.units[:k + 1]

    def suites_upto(self, k: int) -> dict[str, Path]:
        return {u.unit_id: self.suite_set / u.unit_id for u in self._orig_upto(k)}

    def _dropped(self, uid: str) -> list[str]:
        """Cases no gate counts: vacuous (pass on the docs-only repository) and soft (an imported suite's cases that
        break the fixed audits, import_suites)."""
        m = self.meta[uid]
        return sorted(set(m.get("vacuous") or []) | set(m.get("soft") or []))

    def deselect(self, k: int) -> dict[str, list[str]]:
        return {u.unit_id: self._dropped(u.unit_id) for u in self._orig_upto(k)}

    def known(self, k: int) -> list[str]:
        out = []
        for u in self._orig_upto(k):
            vac = set(self._dropped(u.unit_id))
            out += [n for n in self.meta[u.unit_id].get("nodes") or [] if n not in vac]
        return out

    def gate(self, code: Path, k: int, udir: Path) -> GateResult:
        r = run_gate(code, self.suites_upto(k), venv=self.task.venv, work_dir=udir / "gates", timeout=self.cfg.gate_timeout)
        return complete(drop(r, [n for ids in self.deselect(k).values() for n in ids]), self.known(k))

    def columns(self, g: GateResult, unit_id: str) -> Columns:
        if self.single:                                     # one unit: every failure is its own
            return Columns(new=g.failed(), inherited=[], regression=[])
        return columns(g, unit_id, self.baseline)

    def _session(self, role: str, k: int, ws: Path, udir: Path, name: str, instance: str) -> dict[str, Any]:
        if self.ledger.exhausted():
            return {"role": role, "skipped": f"round {self.ledger.exhausted()} exhausted"}
        lease = self.ledger.lease(k, role)
        margin = int(self.cfg.agent_config.get("margin_tokens", 0))
        if lease.tokens_left() < margin:
            # nothing left in the unit's allocation: the session asks the planner before its first call
            self.planner.decide_topup({"lease": lease.lease_id, "unit": k, "role": role, "kind": "tokens",
                                       "amount": float(max(margin * 2, self.ledger.total.tokens // (4 * self.ledger.n_units))),
                                       "reason": f"{role} session start: the unit's allocation is used up", "calls": 0,
                                       "time": time.time()})
        return run_session(role=role, system=role_prompt(role), instance=instance, workspace=ws, venv=self.task.venv,
                           session_dir=udir / name, ledger=self.ledger, lease=lease, model=self.models.agent(),
                           planner=self.planner.decide_topup, summarizer=self.models.summarize, prober=self.prober,
                           agent_config={**self.cfg.agent_config, "probe_every": self.cfg.probe_every},
                           command_timeout=self.cfg.command_timeout, manifest_dir=self.run_dir)

    # ------------------------------------------------------------------ one unit

    def run_unit(self, k: int, code_in: Path) -> Path:
        u = self.task.units[k]
        udir = self.run_dir / "units" / f"{k:02d}_{u.unit_id}"
        ws = copy_code(code_in, udir / "ws")
        copy_code(code_in, self.run_dir / "snapshots" / f"{k:02d}_{u.unit_id}")
        shares = (self.table or {}).get("units", {}).get(u.unit_id)
        alloc = self.ledger.open_unit(k, shares={kk: v for kk, v in shares.items() if kk != "repairs"} if shares else None)
        if shares and "repairs" in shares:
            self.ledger.tighten(k, "repairs", int(shares["repairs"]), reason="budget table", decided_by="table")
        own = self.suites_upto(k) if self.single else {u.unit_id: self.suite_set / u.unit_id}
        self.prober = Prober(ledger=self.ledger, planner=self.planner, workspace=ws, suites=own,
                             deselect={uid: self._dropped(uid) for uid in own},
                             venv=self.task.venv, work_dir=udir / "probes",
                             gate_timeout=self.cfg.probe_timeout) if self.cfg.probe_every else None
        rec: dict[str, Any] = {"unit": u.unit_id, "index": k, "allocation": asdict(alloc), "sessions": [], "gates": [],
                               "repairs": []}
        brief = u.brief()
        rec["sessions"].append(self._session("builder", k, ws, udir, "build", brief))
        g = self.gate(ws, k, udir)
        cols = self.columns(g, u.unit_id)
        rec["gates"].append({"after": "build", **cols.counts(), "seconds": g.seconds, "timed_out": g.timed_out})
        n = 0
        while not self.ledger.exhausted():
            if not cols.passes:
                target = "own"
            elif cols.inherited and self.planner.decide_inherited(k, own_passes=True, n_inherited=len(cols.inherited)):
                target = "inherited"
            else:
                break
            if not self.ledger.can_repair(k):
                break
            self.ledger.use_repair(k)
            n += 1
            _evidence(udir / "repair_evidence", cols, g, self.suites_upto(k), target)
            before = cols.counts()
            s = self._session("repairer", k, ws, udir, f"repair{n}", brief + "\n\nRead ../repair_evidence/failures.md first.")
            rec["sessions"].append(s)
            g = self.gate(ws, k, udir)
            cols = self.columns(g, u.unit_id)
            rec["gates"].append({"after": f"repair{n}", **cols.counts(), "seconds": g.seconds, "timed_out": g.timed_out})
            rec["repairs"].append({"n": n, "target": target, "before": before, "after": cols.counts(),
                                   "exit_status": s.get("exit_status")})
        shutil.rmtree(udir / "repair_evidence", ignore_errors=True)
        rec["passed"] = cols.passes
        rec["columns"] = cols.to_dict()
        rec["exhausted"] = self.ledger.exhausted()
        self.baseline = commit_baseline(self.baseline, g)
        rec["use"] = self.ledger.close_unit(k)
        (udir / "unit.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        self.units.append(rec)
        return ws

    # ------------------------------------------------------------------ the round

    def run(self) -> dict[str, Any]:
        t0 = time.time()
        code = build_workspace(self.task, self.run_dir / "input")
        exhausted_at = None
        for k in range(len(self.task.units)):
            code = self.run_unit(k, code)
            if exhausted_at is None and self.ledger.exhausted():
                exhausted_at = self.task.units[k].unit_id
        final = copy_code(code, self.run_dir / "final")
        last = len(self.task.units) - 1
        g = self.gate(final, last, self.run_dir)
        known = self.known(last)
        passed = sum(1 for n in known if outcome(g.results, n) == "passed")
        sub = submit_check.check(final, package=self.task.package, docs=docs_of(final), suites=self.suites_upto(last),
                                 venv=self.task.venv, work_dir=self.run_dir / "submit")
        rec = {"task": self.task.task_id, "config": self.cfg.to_dict(), "table_version": (self.table or {}).get("version", 0),
               "measures": {"submittable": sub["submittable"],
                            "frozen_pass_rate": round(passed / len(known), 4) if known else 0.0,
                            "frozen_passed": passed, "frozen_tests": len(known),
                            "gated_units": sum(1 for u in self.units if u["passed"]),
                            "exhausted_at": exhausted_at},
               "submit_check": sub, "ledger": self.ledger.snapshot(), "planner_decisions": self.planner.decisions,
               "units": [{k: u[k] for k in ("unit", "passed", "repairs", "gates", "exhausted")} for u in self.units],
               "wall_seconds": round(time.time() - t0, 1)}
        (self.run_dir / "round.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        return rec
