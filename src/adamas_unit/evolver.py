"""The budget evolver between rounds (DESIGN §8.3, §8.5; USING §4 item 9). This file: the mechanical parts.

- ``validate_table``: a proposed budget table passes only when, for every budget kind, the shares sum to at most 1
  (the round's B), every unit's share is at least the floor, every share moved at most ``max_change`` (±30%, D10)
  relatively from the incumbent's (or from the even split for pi_0), and repair limits stay within 0..3.
- ``accept``: the priority rule of D8: (1) the submittable ratio not below the incumbent's; (2) the frozen-suite pass
  rate higher by more than eps_noise; (3) tied on both: more gated units, or less waste. No weighting.

The proposal step (one model call reading the table, round summaries and diagnostics) comes with the diagnostics.
"""

from __future__ import annotations

from typing import Any

SHARE_KINDS = ("tokens", "seconds", "compressions")


def even_table(unit_ids: list[str]) -> dict[str, dict[str, float]]:
    n = len(unit_ids)
    return {u: {k: 1.0 / n for k in SHARE_KINDS} | {"repairs": 3} for u in unit_ids}


def validate_table(proposed: dict[str, dict[str, float]], incumbent: dict[str, dict[str, float]] | None,
                   unit_ids: list[str], *, floor_share: float = 0.05, max_change: float = 0.30,
                   max_repairs: int = 3) -> list[str]:
    """Problems of a proposal ([] = it passes)."""
    inc = incumbent or even_table(unit_ids)
    bad = []
    if set(proposed) != set(unit_ids):
        bad.append(f"units {sorted(set(proposed) ^ set(unit_ids))} missing or unknown")
        return bad
    for k in SHARE_KINDS:
        total = sum(float(proposed[u].get(k, 0)) for u in unit_ids)
        if total > 1.0 + 1e-9:
            bad.append(f"{k}: shares sum to {total:.3f} > 1")
        for u in unit_ids:
            v = float(proposed[u].get(k, 0))
            if v < floor_share - 1e-9:
                bad.append(f"{u}.{k}: {v:.3f} below the floor {floor_share}")
            old = float(inc[u].get(k, 1.0 / len(unit_ids)))
            if old > 0 and abs(v - old) / old > max_change + 1e-9:
                bad.append(f"{u}.{k}: {old:.3f} -> {v:.3f} moves more than {max_change:.0%}")
    for u in unit_ids:
        r = proposed[u].get("repairs", max_repairs)
        if not isinstance(r, int) or not 0 <= r <= max_repairs:
            bad.append(f"{u}.repairs: {r!r} not an integer in 0..{max_repairs}")
    return bad


def accept(candidate: dict[str, Any], incumbent: dict[str, Any], *, eps_noise: float) -> tuple[bool, str]:
    """``candidate`` / ``incumbent``: {"submittable_ratio", "frozen_pass_rate", "gated_units", "waste"} (means over
    the rounds each strategy ran). Returns (accepted, which priority decided)."""
    if candidate["submittable_ratio"] < incumbent["submittable_ratio"]:
        return False, "1: submittable ratio fell"
    d = candidate["frozen_pass_rate"] - incumbent["frozen_pass_rate"]
    if d > eps_noise:
        return True, f"2: frozen pass rate +{d:.4f} > eps {eps_noise}"
    if d < -eps_noise:
        return False, f"2: frozen pass rate {d:.4f} below -eps"
    if candidate["gated_units"] > incumbent["gated_units"]:
        return True, "3: tied on 1-2, more gated units"
    if candidate["gated_units"] == incumbent["gated_units"] and candidate["waste"] < incumbent["waste"]:
        return True, "3: tied on 1-2 and gated units, less waste"
    return False, "3: no improvement beyond noise"
