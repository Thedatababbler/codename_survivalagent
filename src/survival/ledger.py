"""The ledger of one v0.3 round (refactor §2.2, §3.1, §5.3).

One ledger per round. It holds the round's safety limits (never evolved): the token budget B, the compression pool,
the call limits of the three sub-agent kinds and of all sub-agents together, and the floor of every stage. A stage's
allocation is what is left divided evenly over the stages left, never more than what is left minus the floors of the
later stages; what a stage leaves stays in the round.

Tokens are prompt tokens, as in v0.2. Every model call is booked on an *account*:

- ``executor``: the executor's own calls, on the current stage;
- a sub-agent's quota: reserved out of the stage before the sub-agent starts (the executor cannot spend it
  meanwhile), charged as the sub-agent calls the model, and what it does not use is released when it ends;
- side accounts (``management`` = the planner's judgment calls, ``harness`` seconds, ``evolver``): tallied apart,
  never part of B. The closing import fix has its own ledger (v0.2 ``importcheck``).

Everything is an append-only event list written to ``<path>`` after every change. No model call happens here.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SUBAGENT_KINDS = ("review", "repair", "worker")
SIDE = ("management", "harness", "evolver")


class Refused(RuntimeError):
    """A safety limit refuses the request; the text is what the executor is told."""


@dataclass
class Limits:
    """The safety limits of a round (§5.3). Enforced here, never evolved."""
    tokens: int
    seconds: float = 14400.0
    compressions: int = 8
    reviews: int = 4
    repairs: int = 8
    workers: int = 3
    subagents: int = 10
    floor_share: float = 0.05            # of B, kept for every later stage

    def subagent_limit(self, kind: str) -> int:
        return {"review": self.reviews, "repair": self.repairs, "worker": self.workers}[kind]


@dataclass
class Quota:
    """A sub-agent's allowance, reserved out of a stage."""
    quota_id: str
    kind: str
    stage: int
    tokens: int
    used: int = 0
    closed: bool = False

    def left(self) -> int:
        return self.tokens - self.used


@dataclass
class Stage:
    index: int
    allocation: int                      # prior allocation + grants
    prior: int
    used: int = 0                        # executor + sub-agents of this stage
    reserved: int = 0                    # quotas of running sub-agents not yet used
    granted: int = 0
    closed: bool = False
    declared_done: bool = False
    below_minimum: bool = False
    calls: dict[str, int] = field(default_factory=lambda: {k: 0 for k in SUBAGENT_KINDS})


class RoundLedger:
    def __init__(self, limits: Limits, n_stages: int, *, path: str | Path, clock=time.time):
        if n_stages < 1:
            raise ValueError("a round has at least one stage")
        self.limits, self.n_stages, self.path, self.clock = limits, n_stages, Path(path), clock
        self.t0 = clock()
        self.used = 0
        self.compressions = 0
        self.calls = {k: 0 for k in SUBAGENT_KINDS}
        self.handoffs = 0
        self.rollbacks = 0
        self.side = {k: {"tokens": 0, "seconds": 0.0} for k in SIDE}
        self.stages: dict[int, Stage] = {}
        self.quotas: dict[str, Quota] = {}
        self.events: list[dict[str, Any]] = []
        self.current = -1
        self._log("open", limits=asdict(limits), n_stages=n_stages)

    # ------------------------------------------------------------------ state

    def left(self) -> int:
        return self.limits.tokens - self.used

    def seconds_left(self) -> float:
        return self.limits.seconds - (self.clock() - self.t0 - self.side["harness"]["seconds"])

    def floor(self) -> int:
        return int(self.limits.tokens * self.limits.floor_share)

    def later_floors(self, stage: int) -> int:
        return (self.n_stages - stage - 1) * self.floor()

    def stage(self) -> Stage:
        return self.stages[self.current]

    def stage_left(self, stage: int | None = None) -> int:
        """What the executor may still spend in the stage: allocation minus used minus reserved quotas, and never
        more than the round has left."""
        s = self.stages[self.current if stage is None else stage]
        return max(0, min(s.allocation - s.used - s.reserved, self.left() - s.reserved))

    def subagents_used(self) -> int:
        return sum(self.calls.values())

    def exhausted(self) -> str:
        if self.left() <= 0:
            return "tokens"
        if self.seconds_left() <= 0:
            return "seconds"
        return ""

    # ------------------------------------------------------------------ stages

    def open_stage(self, index: int) -> Stage:
        if index in self.stages:
            raise ValueError(f"stage {index} already opened")
        if self.current >= 0 and not self.stages[self.current].closed:
            self.close_stage()
        left_stages = self.n_stages - index
        want = self.left() // left_stages
        cap = max(0, self.left() - self.later_floors(index))
        a = max(0, min(want, cap))
        self.stages[index] = Stage(index=index, allocation=a, prior=a)
        self.current = index
        self._log("open_stage", stage=index, allocation=a)
        return self.stages[index]

    def close_stage(self, *, declared_done: bool = False, below_minimum: bool = False, why: str = "") -> dict[str, Any]:
        s = self.stage()
        for q in self.quotas.values():
            if q.stage == s.index and not q.closed:
                self.release(q.quota_id)
        s.closed, s.declared_done, s.below_minimum = True, declared_done, below_minimum
        rec = {"stage": s.index, "allocation": s.allocation, "used": s.used, "unused": s.allocation - s.used,
               "declared_done": declared_done, "below_minimum": below_minimum, "why": why, "calls": dict(s.calls)}
        self._log("close_stage", **rec)
        return rec

    # ------------------------------------------------------------------ charging

    def charge(self, prompt_tokens: int, completion_tokens: int = 0, *, quota_id: str | None = None,
               what: str = "call") -> None:
        """Book one model call: on the sub-agent's quota when ``quota_id`` is given, else on the executor."""
        n = int(prompt_tokens)
        if quota_id is not None:
            q = self.quotas[quota_id]
            s = self.stages[q.stage]
            q.used += n
            s.reserved = max(0, s.reserved - n)          # the reserved part turns into used
        else:
            s = self.stage()
        s.used += n
        self.used += n
        self._log("charge", account=quota_id or "executor", stage=s.index, what=what, prompt_tokens=n,
                  completion_tokens=int(completion_tokens))

    # ------------------------------------------------------------------ sub-agents (§2.2, §5.3)

    def subagent_cap(self, margin: int = 0) -> int:
        """The most a sub-agent may be given now: what the stage has left, minus what the executor needs to go on
        afterwards (``margin``)."""
        return max(0, self.stage_left() - int(margin))

    def reserve(self, kind: str, tokens: int | None, *, margin: int = 0) -> Quota:
        """Take a sub-agent's quota out of the stage. Refused: no quota named, a call limit reached, nothing to
        give. A quota over the cap is cut to the cap."""
        if kind not in SUBAGENT_KINDS:
            raise Refused(f"unknown sub-agent type {kind!r}: use review, repair or worker")
        if tokens is None or tokens <= 0:
            self._log("subagent_refused", kind=kind, why="no quota")
            raise Refused("a sub-agent call must name its token quota")
        if self.calls[kind] >= self.limits.subagent_limit(kind):
            self._log("subagent_refused", kind=kind, why="kind limit")
            raise Refused(f"the {kind} call limit of this round is reached ({self.calls[kind]}/{self.limits.subagent_limit(kind)})")
        if self.subagents_used() >= self.limits.subagents:
            self._log("subagent_refused", kind=kind, why="total limit")
            raise Refused(f"the sub-agent call limit of this round is reached ({self.subagents_used()}/{self.limits.subagents})")
        cap = self.subagent_cap(margin)
        if cap <= 0:
            self._log("subagent_refused", kind=kind, why="no tokens")
            raise Refused("this stage has no tokens left to give a sub-agent")
        got = int(min(tokens, cap))
        s = self.stage()
        q = Quota(quota_id=f"s{s.index}-{kind}-{len(self.quotas)}", kind=kind, stage=s.index, tokens=got)
        self.quotas[q.quota_id] = q
        s.reserved += got
        s.calls[kind] += 1
        self.calls[kind] += 1
        if kind == "worker":
            self.handoffs += 1
        self._log("reserve", quota=q.quota_id, kind=kind, stage=s.index, requested=int(tokens), cap=cap, tokens=got,
                  cut=tokens > cap)
        return q

    def release(self, quota_id: str) -> int:
        """The sub-agent ended: what it did not use goes back to the stage. Returns the amount returned."""
        q = self.quotas[quota_id]
        if q.closed:
            return 0
        q.closed = True
        back = max(0, q.left())
        s = self.stages[q.stage]
        s.reserved = max(0, s.reserved - back)
        self._log("release", quota=quota_id, used=q.used, returned=back)
        return back

    # ------------------------------------------------------------------ top-ups

    def grant_cap(self) -> int:
        """The most a top-up of the current stage may get: left minus the floors of the later stages, minus what
        the stage still holds."""
        s = self.stage()
        held = max(0, s.allocation - s.used)
        return max(0, self.left() - self.later_floors(s.index) - held)

    def grant(self, amount: int, *, requested: int, reason: str, decided_by: str) -> int:
        cap = self.grant_cap()
        got = int(max(0, min(int(amount), cap)))
        s = self.stage()
        s.allocation += got
        s.granted += got
        self._log("grant", stage=s.index, requested=int(requested), decided=int(amount), cap=cap, granted=got,
                  cut=amount > cap, reason=reason[:300], decided_by=decided_by)
        return got

    # ------------------------------------------------------------------ counters

    def use_compression(self) -> bool:
        if self.compressions >= self.limits.compressions:
            self._log("compression_refused")
            return False
        self.compressions += 1
        self._log("compression", n=self.compressions)
        return True

    def note_rollback(self, **data: Any) -> None:
        self.rollbacks += 1
        self._log("rollback", **data)

    def side_charge(self, account: str, *, tokens: int = 0, seconds: float = 0.0, what: str = "") -> None:
        if account not in SIDE:
            raise ValueError(account)
        self.side[account]["tokens"] += int(tokens)
        self.side[account]["seconds"] += float(seconds)
        self._log("side", account=account, tokens=int(tokens), seconds=round(float(seconds), 2), what=what)

    def note(self, event: str, **data: Any) -> None:
        self._log(event, **data)

    # ------------------------------------------------------------------ persistence

    def snapshot(self) -> dict[str, Any]:
        return {"limits": asdict(self.limits), "used": self.used, "left": self.left(), "compressions": self.compressions,
                "calls": dict(self.calls), "subagents": self.subagents_used(), "handoffs": self.handoffs,
                "rollbacks": self.rollbacks, "n_stages": self.n_stages, "current": self.current,
                "stages": {i: asdict(s) for i, s in self.stages.items()},
                "quotas": {k: asdict(q) for k, q in self.quotas.items()}, "side": self.side}

    def _log(self, event: str, **data: Any) -> None:
        self.events.append({"t": round(self.clock() - self.t0, 3), "event": event, **data})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"events": self.events, "state": self.snapshot()}, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)
