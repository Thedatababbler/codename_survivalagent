"""The budget ledger of one round (docs/DESIGN.md §5).

One ledger per round and repository. It holds the round budget B = (tokens, seconds, compressions, repairs), hands
each unit its prior allocation (a share of what is left, from the budget table or evenly), leases parts of it to
sessions, books what they use, takes back what a unit leaves, and caps every top-up mechanically: a grant never
exceeds what is left minus the floors kept for the later units (§5.3). Side accounts (authoring, replay, management,
harness) are tallied apart and never touch B (§13.1); management has its own cap (§11).

Wall time: the agents' clock is the round's elapsed time minus the harness overhead (progress probes, §8.4) -- the
probes are ours, the agents do not pay for them.

Everything is an append-only event list written to ``<path>`` after every change, so a round can be inspected or
resumed. No model call happens here.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("tokens", "seconds", "compressions", "repairs")
SIDE = ("authoring", "replay", "management", "harness")


class BudgetExhausted(RuntimeError):
    """The round budget of one kind is used up (DESIGN §5.5)."""


@dataclass
class Budget:
    tokens: int = 0
    seconds: float = 0.0
    compressions: int = 0
    repairs: int = 0

    def get(self, kind: str) -> float:
        return getattr(self, kind)


@dataclass
class Lease:
    """A session's allowance of one unit's allocation (tokens and seconds; compressions and repairs are counted at
    the unit / round level)."""
    lease_id: str
    unit: int
    role: str
    tokens: int
    seconds: float
    used_tokens: int = 0
    started: float = field(default_factory=time.time)
    closed: bool = False
    harness_seconds: float = 0.0       # probes during the session: not the session's time

    def tokens_left(self) -> int:
        return self.tokens - self.used_tokens


class Ledger:
    def __init__(self, total: Budget, n_units: int, *, path: str | Path, floor_share: float = 0.05,
                 management_cap_share: float = 0.02, max_repairs_per_unit: int = 3, clock=time.time):
        if n_units < 1:
            raise ValueError("a round has at least one unit")
        self.total = total
        self.n_units = n_units
        self.path = Path(path)
        self.floor_share = floor_share                    # per later unit, of B (the mechanical cap, §5.3)
        self.management_cap = int(total.tokens * management_cap_share)
        self.max_repairs_per_unit = max_repairs_per_unit
        self.clock = clock
        self.t0 = clock()
        self.used = Budget()
        self.side: dict[str, dict[str, float]] = {k: {"tokens": 0, "seconds": 0.0} for k in SIDE}
        self.alloc: dict[int, Budget] = {}                # prior allocation (+ grants) of each opened unit
        self.unit_used: dict[int, Budget] = {}
        self.leases: dict[str, Lease] = {}
        self.events: list[dict[str, Any]] = []
        self._log("open", total=asdict(total), n_units=n_units, floor_share=floor_share,
                  management_cap=self.management_cap)

    # ------------------------------------------------------------------ state

    def elapsed_agent_seconds(self) -> float:
        """The agents' clock: elapsed round time minus the harness overhead."""
        return self.clock() - self.t0 - self.side["harness"]["seconds"]

    def left(self, kind: str) -> float:
        if kind == "seconds":
            return self.total.seconds - self.elapsed_agent_seconds()
        return self.total.get(kind) - self.used.get(kind)

    def exhausted(self) -> str:
        """'' or the first kind of the round budget that is used up (tokens or time; DESIGN §5.5)."""
        for k in ("tokens", "seconds"):
            if self.left(k) <= 0:
                return k
        return ""

    def floor(self, kind: str) -> float:
        return self.total.get(kind) * self.floor_share

    def grant_cap(self, unit: int, kind: str) -> float:
        """The most any top-up for ``unit`` may get: left minus the floors of the later units, minus what the
        unit's open allocation still holds unused (that is spent first)."""
        later = self.n_units - unit - 1
        held = max(0.0, self.alloc_left(unit, kind)) if unit in self.alloc else 0.0
        return max(0.0, self.left(kind) - later * self.floor(kind) - held)

    def alloc_left(self, unit: int, kind: str) -> float:
        a, u = self.alloc[unit], self.unit_used[unit]
        if kind == "seconds":
            return a.seconds - u.seconds
        return a.get(kind) - u.get(kind)

    # ------------------------------------------------------------------ units

    def open_unit(self, unit: int, shares: dict[str, float] | None = None) -> Budget:
        """The prior allocation of ``unit``: ``shares`` (fractions of B, from the budget table), else what is left
        divided evenly over the units left (π_0). Never more than the grant cap allows."""
        if unit in self.alloc:
            raise ValueError(f"unit {unit} already opened")
        units_left = self.n_units - unit
        b = Budget()
        for k in KINDS:
            if k == "repairs":
                continue                                   # repairs are counted per unit and per round, not leased
            if shares and k in shares:
                want = self.total.get(k) * float(shares[k])
            else:
                want = self.left(k) / units_left
            cap = max(0.0, self.left(k) - (units_left - 1) * self.floor(k))
            setattr(b, k, type(getattr(b, k))(max(0.0, min(want, cap))))
        b.repairs = self.max_repairs_per_unit
        self.alloc[unit] = b
        self.unit_used[unit] = Budget()
        self._log("open_unit", unit=unit, allocation=asdict(b), shares=shares or "even")
        return b

    def close_unit(self, unit: int) -> dict[str, Any]:
        """What the unit used and left; the unused part simply stays in the round's 'left'."""
        for lid, ls in self.leases.items():
            if ls.unit == unit and not ls.closed:
                self.close_lease(lid)
        rec = {"unit": unit, "allocation": asdict(self.alloc[unit]), "used": asdict(self.unit_used[unit]),
               "unused": {k: self.alloc_left(unit, k) for k in ("tokens", "seconds", "compressions")}}
        self._log("close_unit", **rec)
        return rec

    # ------------------------------------------------------------------ sessions

    def lease(self, unit: int, role: str, *, tokens: int | None = None, seconds: float | None = None) -> Lease:
        """A session's allowance out of the unit's allocation (default: all that is left of it)."""
        if self.exhausted():
            raise BudgetExhausted(self.exhausted())
        t = int(min(tokens if tokens is not None else 1e18, self.alloc_left(unit, "tokens")))
        s = float(min(seconds if seconds is not None else 1e18, self.alloc_left(unit, "seconds"), self.left("seconds")))
        ls = Lease(lease_id=f"u{unit}-{role}-{len(self.leases)}", unit=unit, role=role, tokens=max(0, t), seconds=max(0.0, s),
                   started=self.clock())
        self.leases[ls.lease_id] = ls
        self._log("lease", lease=ls.lease_id, unit=unit, role=role, tokens=ls.tokens, seconds=ls.seconds)
        return ls

    def charge(self, lease_id: str, prompt_tokens: int, completion_tokens: int = 0, *, what: str = "call") -> None:
        ls = self.leases[lease_id]
        n = int(prompt_tokens)
        ls.used_tokens += n
        self.used.tokens += n
        self.unit_used[ls.unit].tokens += n
        self._log("charge", lease=lease_id, what=what, prompt_tokens=n, completion_tokens=int(completion_tokens))

    def seconds_left(self, lease_id: str) -> float:
        ls = self.leases[lease_id]
        own = ls.seconds - (self.clock() - ls.started - ls.harness_seconds)
        return min(own, self.left("seconds"))

    def close_lease(self, lease_id: str) -> None:
        ls = self.leases[lease_id]
        if ls.closed:
            return
        ls.closed = True
        dt = self.clock() - ls.started - ls.harness_seconds
        self.unit_used[ls.unit].seconds += dt
        self.used.seconds += dt
        self._log("close_lease", lease=lease_id, used_tokens=ls.used_tokens, seconds=round(dt, 2))

    # ------------------------------------------------------------------ top-ups (§5.3)

    def grant(self, lease_id: str, kind: str, amount: float, *, requested: float, reason: str, decided_by: str) -> float:
        """Apply a planner decision, cut to the mechanical cap; returns what was granted."""
        if kind not in ("tokens", "seconds"):
            raise ValueError("top-ups are for tokens or seconds")
        ls = self.leases[lease_id]
        cap = self.grant_cap(ls.unit, kind)
        got = max(0.0, min(float(amount), cap))
        if kind == "tokens":
            got = int(got)
            ls.tokens += got
            self.alloc[ls.unit].tokens += got
        else:
            ls.seconds += got
            self.alloc[ls.unit].seconds += got
        self._log("grant", lease=lease_id, unit=ls.unit, kind=kind, requested=requested, decided=amount, cap=cap,
                  granted=got, cut=float(amount) > cap, reason=reason[:500], decided_by=decided_by)
        return got

    def reclaim(self, lease_id: str, *, reason: str) -> None:
        """Early take-back (no-progress loop): the lease ends, its unused tokens stay in the round."""
        ls = self.leases[lease_id]
        self._log("reclaim", lease=lease_id, unused_tokens=ls.tokens_left(), reason=reason[:300])
        self.close_lease(lease_id)

    # ------------------------------------------------------------------ compressions and repairs

    def use_compression(self, unit: int) -> bool:
        """One compression from the round pool and the unit's allowance; False when none is left."""
        if self.left("compressions") < 1 or self.alloc_left(unit, "compressions") < 1:
            self._log("compression_refused", unit=unit)
            return False
        self.used.compressions += 1
        self.unit_used[unit].compressions += 1
        self._log("compression", unit=unit)
        return True

    def can_repair(self, unit: int) -> bool:
        return self.left("repairs") >= 1 and self.unit_used[unit].repairs < self.alloc[unit].repairs

    def use_repair(self, unit: int) -> None:
        if not self.can_repair(unit):
            raise BudgetExhausted("repairs")
        self.used.repairs += 1
        self.unit_used[unit].repairs += 1
        self._log("repair", unit=unit, unit_repairs=self.unit_used[unit].repairs, round_repairs=self.used.repairs)

    def tighten(self, unit: int, kind: str, limit: int, *, reason: str, decided_by: str) -> None:
        """The planner may lower the unit's repair or compression allowance, never raise it (§5.3)."""
        if kind not in ("repairs", "compressions"):
            raise ValueError(kind)
        cur = int(self.alloc[unit].get(kind))
        new = max(int(self.unit_used[unit].get(kind)), min(cur, int(limit)))
        setattr(self.alloc[unit], kind, new)
        self._log("tighten", unit=unit, kind=kind, before=cur, after=new, reason=reason[:300], decided_by=decided_by)

    # ------------------------------------------------------------------ side accounts (§13.1)

    def side_charge(self, account: str, *, tokens: int = 0, seconds: float = 0.0, what: str = "",
                    lease_id: str | None = None) -> None:
        """Book on a side account. Harness seconds during a session (``lease_id``) come off that session's clock too."""
        if account not in SIDE:
            raise ValueError(account)
        if account == "management" and self.side["management"]["tokens"] + tokens > self.management_cap:
            self._log("management_cap", tokens=tokens, cap=self.management_cap)
            raise BudgetExhausted("management")
        self.side[account]["tokens"] += int(tokens)
        self.side[account]["seconds"] += float(seconds)
        if account == "harness" and lease_id in self.leases:
            self.leases[lease_id].harness_seconds += float(seconds)
        self._log("side", account=account, tokens=int(tokens), seconds=round(float(seconds), 2), what=what)

    def management_left(self) -> int:
        return int(self.management_cap - self.side["management"]["tokens"])

    # ------------------------------------------------------------------ persistence

    def note(self, event: str, **data: Any) -> None:
        """A record without a change of state (a refused request, a planner decision, a gate result)."""
        self._log(event, **data)

    def snapshot(self) -> dict[str, Any]:
        """The state the planner sees (numbers only, no code)."""
        return {"total": asdict(self.total), "used": asdict(self.used),
                "left": {k: self.left(k) for k in KINDS}, "n_units": self.n_units,
                "units": {u: {"allocation": asdict(self.alloc[u]), "used": asdict(self.unit_used[u])} for u in self.alloc},
                "side": self.side, "management_left": self.management_left()}

    def _log(self, event: str, **data: Any) -> None:
        self.events.append({"t": round(self.clock() - self.t0, 3), "event": event, **data})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"events": self.events, "state": self.snapshot() if self.alloc is not None else {}},
                                  indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)
