"""The budget planner's own state across rounds (DESIGN §8.1, §8.2; USING §4 item 10).

``outputs/planner/<task>/``: the task's budget table (versioned: ``table_v<N>.json`` and ``current``), the general
rules (``rules.json``; AdaMAS's trial / active / retired states), round summaries (``rounds.jsonl``, numbers only).
This is AdaMAS-unit's only memory that crosses rounds. No agent may see it: it lies outside every workspace and the
sandbox grants no agent any of ``outputs/``; besides that, every file here carries a canary
(``CANARY-PL-<16 hex>``) and the breach scanner voids a run in which a canary turns up in anything an agent read or
wrote (``orchestra.sandbox.breach.canary_hits``).

A budget table: ``{"version": N, "units": {unit_id: {"tokens": share, "seconds": share, "compressions": share,
"repairs": limit}}, "approval": {...optional}, "canary": ...}``; shares are fractions of B, repairs a limit within 3
(DESIGN §8.1); approval: the ledger's approval parameters (``ledger.APPROVAL_DEFAULTS``; absent = the defaults).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = ROOT / "outputs" / "planner"
RULE_STATES = ("trial", "active", "retired")


class PlannerState:
    def __init__(self, task_id: str, root: Path = STATE_ROOT):
        self.dir = Path(root) / task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        cf = self.dir / "CANARY"
        if not cf.is_file():
            cf.write_text(f"CANARY-PL-{secrets.token_hex(8)}\n", encoding="utf-8")
        self.canary = cf.read_text(encoding="utf-8").strip()

    # ------------------------------------------------------------------ budget table

    def table(self) -> dict[str, Any] | None:
        cur = self.dir / "current"
        if not cur.is_file():
            return None                                   # pi_0: even split
        return json.loads((self.dir / cur.read_text(encoding="utf-8").strip()).read_text(encoding="utf-8"))

    def table_version(self, version: int) -> dict[str, Any]:
        """A stored table by version (a candidate under trial is not the current one)."""
        return json.loads((self.dir / f"table_v{version}.json").read_text(encoding="utf-8"))

    def write_table(self, units: dict[str, dict[str, float]], *, source: str,
                    approval: dict[str, Any] | None = None) -> dict[str, Any]:
        # the next free version: a rejected candidate is never current, and must not be overwritten
        vs = [int(p.stem.removeprefix("table_v")) for p in self.dir.glob("table_v*.json")
              if p.stem.removeprefix("table_v").isdigit()]
        v = max(vs, default=0) + 1
        t = {"version": v, "units": units, "source": source, "canary": self.canary}
        if approval:
            t["approval"] = approval                      # absent = the defaults (ledger.APPROVAL_DEFAULTS)
        (self.dir / f"table_v{v}.json").write_text(json.dumps(t, indent=1), encoding="utf-8")
        return t

    def set_current(self, version: int) -> None:
        if not (self.dir / f"table_v{version}.json").is_file():
            raise FileNotFoundError(version)
        (self.dir / "current").write_text(f"table_v{version}.json\n", encoding="utf-8")

    def shares(self, unit_id: str) -> dict[str, float] | None:
        t = self.table()
        return None if t is None else (t.get("units") or {}).get(unit_id)

    # ------------------------------------------------------------------ rules and round summaries

    def rules(self) -> list[dict[str, Any]]:
        p = self.dir / "rules.json"
        return json.loads(p.read_text(encoding="utf-8")).get("rules", []) if p.is_file() else []

    def save_rules(self, rules: list[dict[str, Any]]) -> None:
        for r in rules:
            if r.get("status") not in RULE_STATES:
                raise ValueError(f"rule state {r.get('status')!r}")
        (self.dir / "rules.json").write_text(json.dumps({"rules": rules, "canary": self.canary}, indent=1), encoding="utf-8")

    def add_summary(self, summary: dict[str, Any]) -> None:
        with (self.dir / "rounds.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**summary, "canary": self.canary}, default=str) + "\n")

    def summaries(self, *, include_invalid: bool = False) -> list[dict[str, Any]]:
        """Round summaries; rounds marked ``invalid`` (a pipeline defect made their measures meaningless) are left
        out unless asked for."""
        p = self.dir / "rounds.jsonl"
        out = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.is_file() else []
        return out if include_invalid else [x for x in out if not x.get("invalid")]

    def mark_invalid(self, rounds: set[str], reason: str) -> int:
        """Mark the summaries of ``rounds`` invalid (kept in the file, with the reason); returns how many."""
        p = self.dir / "rounds.jsonl"
        rows = self.summaries(include_invalid=True)
        n = 0
        for r in rows:
            if r.get("round") in rounds and not r.get("invalid"):
                r["invalid"], n = reason, n + 1
        p.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
        return n
