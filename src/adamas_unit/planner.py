"""The budget planner inside a round (DESIGN §5.3, §8.1; USING §4 item 2).

It decides four things, and only from numbers: the ledger snapshot, the progress metrics the harness measured
(gate failures over the session's probes, code changed since the last probe, what the unit used), the task's budget
table and the enabled general rules. It never sees code, tests or held-out data.

1. a top-up request: grant how much (``decide_topup``);
2. inherited failures (column 2): spend a repair on them or not (``decide_inherited``);
3. a no-progress loop: reclaim the session's budget early (``decide_reclaim``);
4. tighten the unit's repair or compression allowance within its limit (any decision may carry ``tighten``).

Every decision goes through the ledger: a grant is cut to the mechanical cap (``Ledger.grant``), a tightening can
only lower (``Ledger.tighten``). The planner's own tokens are booked on the management account, which has its cap
(DESIGN §11); when it is used up, or the model's answer cannot be parsed, the fixed rule below decides instead and the
record says so.

``FixedPolicy`` is that rule and the planner of the positive control: grant at most the request and at most one
more prior allocation of the unit, only while the gate failures are falling or code is still changing; repair
inherited failures only when the unit's own gate passes and repairs are left; reclaim after ``stall_probes`` probes
with no code change and no change in failures.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from adamas_unit.ledger import BudgetExhausted, Ledger


@dataclass
class Probe:
    calls: int
    failures: int | None          # the unit's suite on a snapshot copy; None when the probe could not run
    files_changed: int
    lines_changed: int
    seconds: float


@dataclass
class SessionProgress:
    lease_id: str
    unit: int
    role: str
    probes: list[Probe] = field(default_factory=list)

    def improving(self, window: int = 3) -> bool:
        f = [p.failures for p in self.probes[-window:] if p.failures is not None]
        return len(f) >= 2 and f[-1] < f[0]

    def changing(self, window: int = 2) -> bool:
        return any(p.lines_changed > 0 for p in self.probes[-window:])

    def stalled(self, n: int) -> bool:
        """The last ``n`` probes: no code change and the failure count unchanged."""
        last = self.probes[-n:]
        if len(last) < n:
            return False
        fails = {p.failures for p in last}
        return all(p.lines_changed == 0 for p in last) and len(fails) == 1

    def to_dict(self) -> dict[str, Any]:
        return {"lease": self.lease_id, "unit": self.unit, "role": self.role,
                "probes": [p.__dict__ for p in self.probes[-8:]], "improving": self.improving(),
                "changing": self.changing()}


@dataclass
class FixedPolicy:
    topup_factor: float = 1.0       # at most this many prior allocations of the unit per top-up
    stall_probes: int = 3

    def topup(self, req: dict[str, Any], ledger: Ledger, prog: SessionProgress | None) -> tuple[float, str]:
        if prog is not None and prog.probes and not (prog.improving() or prog.changing()):
            return 0.0, "fixed rule: no measurable progress"
        base = ledger.alloc[req["unit"]].get(req["kind"]) if req["unit"] in ledger.alloc else 0
        return min(float(req["amount"]), self.topup_factor * float(base)), "fixed rule: progress, up to one allocation"

    def inherited(self, unit: int, ledger: Ledger, own_passes: bool, n_inherited: int) -> tuple[bool, str]:
        ok = own_passes and n_inherited > 0 and ledger.can_repair(unit)
        return ok, "fixed rule: own gate passes, repairs left" if ok else "fixed rule: not now"

    def reclaim(self, prog: SessionProgress) -> tuple[bool, str]:
        s = prog.stalled(self.stall_probes)
        return s, f"fixed rule: {self.stall_probes} probes without change" if s else "fixed rule: progress"


SYSTEM = """You are the budget planner of a multi-stage code-generation run. The repository is built unit by unit;
every unit is built by one agent session, checked by a frozen acceptance gate and repaired at most a few times. The
whole repository shares one budget (tokens, wall-clock seconds, context compressions, repairs). You never see code
or tests: you decide only from the numbers below. Spend where it changes the outcome: a session whose gate failures
are falling or whose code is still changing is worth more budget; a session that changes nothing is not. Later units
need budget too, and the final code must be submittable. The ledger cuts any grant to its mechanical cap, so state
what the work is worth, not what you hope to get.

Answer with one JSON object and nothing else."""

SCHEMAS = {
    "topup": '{"grant": <number, 0 to refuse>, "reason": "<one sentence>", '
             '"tighten": {"repairs": <int, optional>, "compressions": <int, optional>}}',
    "inherited": '{"repair": <true|false>, "reason": "<one sentence>", "tighten": {...optional}}',
    "reclaim": '{"reclaim": <true|false>, "reason": "<one sentence>"}',
}


def _json_of(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


class BudgetPlanner:
    """``complete(messages) -> (text, prompt_tokens, completion_tokens)``; None: the fixed rule only."""

    def __init__(self, ledger: Ledger, *, complete: Callable[[list[dict]], tuple[str, int, int]] | None = None,
                 table: dict[str, Any] | None = None, rules: list[dict[str, Any]] | None = None,
                 fixed: FixedPolicy | None = None, progress_of: Callable[[str], SessionProgress | None] | None = None):
        self.ledger = ledger
        self.complete = complete
        self.table = table or {}
        self.rules = [r for r in (rules or []) if r.get("status") == "active"]
        self.fixed = fixed or FixedPolicy()
        self.progress_of = progress_of or (lambda lid: None)
        self.decisions: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ model call

    def _ask(self, kind: str, facts: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if self.complete is None:
            return None, "fixed"
        if self.ledger.management_left() <= 0:
            return None, "fixed (management budget used up)"
        user = (f"Decision: {kind}\nAnswer schema: {SCHEMAS[kind]}\n\n"
                f"Facts:\n{json.dumps(facts, indent=1, default=str)}\n\n"
                f"Budget table for this task: {json.dumps(self.table) if self.table else 'none (even split)'}\n"
                f"Active general rules: {json.dumps([r.get('text') for r in self.rules]) if self.rules else 'none'}")
        try:
            text, p, c = self.complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        except Exception as e:                                   # the channel failed: the fixed rule decides
            return None, f"fixed (planner call failed: {type(e).__name__})"
        try:
            self.ledger.side_charge("management", tokens=p, what=f"planner {kind}")
        except BudgetExhausted:
            return None, "fixed (management budget used up)"
        d = _json_of(text)
        return d, "model" if d is not None else "fixed (unparsable answer)"

    def _facts(self, unit: int, lease_id: str | None = None) -> dict[str, Any]:
        snap = self.ledger.snapshot()
        prog = self.progress_of(lease_id) if lease_id else None
        return {"unit": unit, "n_units": self.ledger.n_units, "left": snap["left"], "used": snap["used"],
                "unit_allocation": snap["units"].get(unit, {}).get("allocation"),
                "unit_used": snap["units"].get(unit, {}).get("used"),
                "grant_cap_tokens": self.ledger.grant_cap(unit, "tokens"),
                "grant_cap_seconds": self.ledger.grant_cap(unit, "seconds"),
                "repairs_left_unit": self.ledger.alloc[unit].repairs - self.ledger.unit_used[unit].repairs if unit in self.ledger.alloc else None,
                "progress": prog.to_dict() if prog else None}

    def _tighten(self, unit: int, d: dict[str, Any] | None, by: str) -> None:
        for k, v in ((d or {}).get("tighten") or {}).items():
            if k in ("repairs", "compressions") and isinstance(v, (int, float)):
                self.ledger.tighten(unit, k, int(v), reason=str((d or {}).get("reason", ""))[:300], decided_by=by)

    def _record(self, **rec: Any) -> None:
        self.decisions.append(rec)

    # ------------------------------------------------------------------ the four decisions

    def decide_topup(self, req: dict[str, Any]) -> str:
        """The agent's ``request_budget``: returns the text the agent sees."""
        unit, lid = req["unit"], req["lease"]
        facts = {**self._facts(unit, lid), "request": {k: req[k] for k in ("kind", "amount", "reason", "role", "calls")}}
        d, by = self._ask("topup", facts)
        if d is not None and isinstance(d.get("grant"), (int, float)):
            amount, reason = max(0.0, float(d["grant"])), str(d.get("reason") or "")
            self._tighten(unit, d, by)
        else:
            amount, reason = self.fixed.topup(req, self.ledger, self.progress_of(lid))
        got = self.ledger.grant(lid, req["kind"], min(amount, float(req["amount"])), requested=req["amount"],
                                reason=reason, decided_by=by) if amount > 0 else 0
        if amount <= 0:
            self.ledger.note("refuse", lease=lid, unit=unit, kind=req["kind"], requested=req["amount"],
                             reason=reason[:500], decided_by=by)
        self._record(decision="topup", unit=unit, lease=lid, requested=req["amount"], kind=req["kind"], decided=amount,
                     granted=got, by=by, reason=reason)
        # the agent sees the amount only: the planner's reason may carry what it knows from earlier rounds (DESIGN §8.2)
        unit_word = "tokens" if req["kind"] == "tokens" else "seconds"
        return f"request_budget: granted {got:g} {unit_word}." if got else "request_budget: refused."

    def decide_inherited(self, unit: int, *, own_passes: bool, n_inherited: int) -> bool:
        mode = self.ledger.approval["inherited"]
        if mode != "planner":                                    # the table's approval parameter decides
            d, by = None, f"table ({mode})"
            yes, reason = (False, "table: inherited failures are not repaired") if mode == "never" else \
                self.fixed.inherited(unit, self.ledger, own_passes, n_inherited)
        else:
            facts = {**self._facts(unit), "own_gate_passes": own_passes, "inherited_failures": n_inherited}
            d, by = self._ask("inherited", facts)
        if mode != "planner":
            pass
        elif d is not None and isinstance(d.get("repair"), bool):
            yes, reason = d["repair"], str(d.get("reason") or "")
            self._tighten(unit, d, by)
        else:
            yes, reason = self.fixed.inherited(unit, self.ledger, own_passes, n_inherited)
        yes = yes and self.ledger.can_repair(unit) and n_inherited > 0
        self.ledger.note("inherited_decision", unit=unit, repair=yes, n_inherited=n_inherited, decided_by=by, reason=reason[:300])
        self._record(decision="inherited", unit=unit, repair=yes, by=by, reason=reason)
        return yes

    def decide_reclaim(self, prog: SessionProgress) -> bool:
        """Called by the progress probe; only a stalled session is ever put to the planner."""
        if not prog.stalled(self.fixed.stall_probes):
            return False
        d, by = self._ask("reclaim", self._facts(prog.unit, prog.lease_id))
        if d is not None and isinstance(d.get("reclaim"), bool):
            yes, reason = d["reclaim"], str(d.get("reason") or "")
        else:
            yes, reason = self.fixed.reclaim(prog)
        self._record(decision="reclaim", unit=prog.unit, lease=prog.lease_id, reclaim=yes, by=by, reason=reason)
        return yes
