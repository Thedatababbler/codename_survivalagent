"""The budget evolver between rounds (DESIGN §8.3, §8.5; USING §4 item 9). This file: the mechanical parts.

- ``validate_table``: a proposed budget table passes only when, for every budget kind, the shares sum to at most 1
  (the round's B), every unit's share is at least the floor, every share moved at most ``max_change`` (±30%, D10)
  relatively from the incumbent's (or from the even split for pi_0), and repair limits stay within 0..3.
- ``accept``: the priority rule of D8: (1) the submittable ratio not below the incumbent's; (2) the frozen-suite pass
  rate higher by more than eps_noise; (3) tied on both: more gated units, or less waste. No weighting.

- ``propose``: the proposal step (DESIGN §8.5 step 2): one model call reads the incumbent table and the records of
  earlier rounds of the task under the same B (numbers only: allocations, use, gate counts, top-up decisions, the
  ledger diagnostics) and returns a table, its reason, and 0-2 general rules (E1: recorded as trial, never applied,
  D12). The prompt states the mechanics and the constraints and nothing about how to divide (2026-11-07 user rule:
  whether the evolver finds the pattern itself is the question E1 answers). An invalid table goes back once with
  its problems.
- ``run_stats`` / ``better`` / ``decide``: the E1 trial of a candidate (2026-11-07 user rule): 2 rounds; both better
  than the incumbent -> accepted; neither -> rejected; one -> a 3rd round, accepted when 2 of 3 are better.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

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


# ---------------------------------------------------------------------------------------------- the proposal step

SYSTEM = """You are the budget evolver of a multi-stage code-generation system. A repository is built in a fixed
sequence of units. Each unit is built by one agent session, checked by a frozen acceptance gate (its own tests plus the
earlier units' tests) and, while its own tests fail, repaired by further sessions up to its repair limit. All units of
one run (a round) share one total budget B of tokens, wall-clock seconds, context compressions and repairs.

How a budget table is used in a round:
- When a unit starts, its allocation of each kind is its share x B, cut so that what is left of the round budget
  still covers the floor of every later unit. A unit's repair limit caps its repair sessions.
- A session stops when its allocation is used up, or when fewer tokens are left than one model call may need (the
  margin). It may then request a top-up; a planner grants or refuses it from what is left of the round budget, cut
  to a mechanical cap (left minus the floors of the later units). The planner sees the budget table.
- What a unit leaves unused stays in the round budget for later units.
- Without a table (the incumbent pi_0), each unit gets what is left of the round budget divided evenly over the
  units left, and up to 3 repairs.

Rounds are compared by, in this order: the submittable ratio (the final code imports, its documented names import,
the suites collect); the pass rate of the final code on all units' frozen tests, where a difference must exceed the
noise between repeated rounds; then the number of units whose gate passed, and waste (allocations left unused,
repairs that changed nothing, sessions without progress).

You see only numbers from the ledger: no code, no tests. Propose the budget table for the next round. Constraints
(checked mechanically; a table that breaks one is returned to you once with the problems):
- every unit appears, with "tokens", "seconds", "compressions" (shares of B) and "repairs" (an integer 0..3);
- for each kind the shares sum to at most 1, and every share is at least the floor;
- every share moves at most the given fraction relative to the incumbent's share (pi_0's shares count as 1/n).

In "reason", say which numbers in the records lead to each change. You may also state at most 2 general rules
(short sentences that would hold for other tasks); they are only recorded.

Answer with one JSON object and nothing else:
{"table": {"<unit id>": {"tokens": <share>, "seconds": <share>, "compressions": <share>, "repairs": <int>}, ...},
 "reason": "<text>", "rules": ["<rule>", ...]}"""


def _no_ids(x: Any) -> Any:
    """Drop strings that look like test node ids (never needed: the evolver sees counts)."""
    if isinstance(x, dict):
        return {k: _no_ids(v) for k, v in x.items() if not (isinstance(v, str) and "::" in v)}
    if isinstance(x, list):
        return [_no_ids(v) for v in x if not (isinstance(v, str) and "::" in v)]
    return x


def round_record(run_dir: Path, diagnostics: dict[str, Any]) -> dict[str, Any]:
    """What the evolver reads of one finished round: numbers only."""
    run_dir = Path(run_dir)
    rnd = json.loads((run_dir / "round.json").read_text(encoding="utf-8"))
    B = rnd["config"]["budget"]
    empty = {x["lease"] for x in diagnostics.get("session_started_empty") or []}
    units = []
    for f in sorted((run_dir / "units").glob("*/unit.json")):
        u = json.loads(f.read_text(encoding="utf-8"))
        use = u.get("use") or {}
        units.append({
            "unit": u["unit"], "index": u["index"],
            "allocation_tokens": round(u["allocation"]["tokens"]), "allocation_share": round(u["allocation"]["tokens"] / B["tokens"], 4),
            "allocation_after_topups_tokens": round((use.get("allocation") or {}).get("tokens", 0)),
            "used_tokens": round((use.get("used") or {}).get("tokens", 0)),
            "unused_tokens": round((use.get("unused") or {}).get("tokens", 0)),
            "used_seconds": round((use.get("used") or {}).get("seconds", 0), 1),
            "repair_limit": u["allocation"].get("repairs"), "repairs_used": len(u["repairs"]),
            "gate_passed": u["passed"], "gates": [{k: g[k] for k in ("after", "new", "inherited", "regression")} for g in u["gates"]],
            "sessions": [{"role": s.get("role"), "exit": s.get("exit_status") or s.get("skipped"),
                          "tokens": s.get("used_tokens"),
                          "started_empty": s["started_empty"] if s.get("started_empty") is not None else s.get("lease") in empty}
                         for s in u["sessions"]],
        })
    topups = [{k: d.get(k) for k in ("unit", "requested", "granted")} for d in rnd.get("planner_decisions") or []
              if d.get("decision") == "topup"]
    diag = {k: v for k, v in diagnostics.items() if k not in ("measures",)}
    return _no_ids({"round": rnd.get("round_id") or run_dir.parent.name, "table_version": rnd.get("table_version", 0),
                    "budget_B": {k: B[k] for k in ("tokens", "seconds", "compressions", "repairs")},
                    "measures": rnd["measures"], "units": units, "topups": topups, "diagnostics": diag})


def evolver_input(incumbent: dict[str, Any] | None, records: list[dict[str, Any]], unit_ids: list[str], *,
                  floor_share: float, max_change: float) -> str:
    inc = {"name": "pi_0 (no table)", "table": None} if not incumbent else \
        {"name": f"table v{incumbent.get('version')}", "table": incumbent.get("units")}
    return (f"Units in order: {json.dumps(unit_ids)}\n"
            f"Floor share: {floor_share}; maximum move per share: {max_change:.0%} of the incumbent's share\n\n"
            f"Incumbent: {json.dumps(inc)}\n\n"
            f"Records of earlier rounds of this task (same B), oldest first:\n{json.dumps(records, indent=1, default=str)}")


def _json_obj(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def propose(complete: Callable[[list[dict]], tuple[str, int, int]], incumbent: dict[str, Any] | None,
            records: list[dict[str, Any]], unit_ids: list[str], *, floor_share: float = 0.05,
            max_change: float = 0.30, attempts: int = 2) -> dict[str, Any]:
    """{"table" (None when no valid one), "reason", "rules", "problems" per attempt, "tokens", "raw"}."""
    user = evolver_input(incumbent, records, unit_ids, floor_share=floor_share, max_change=max_change)
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    out: dict[str, Any] = {"table": None, "reason": "", "rules": [], "problems": [], "tokens": {"prompt": 0, "completion": 0},
                           "raw": []}
    inc_units = (incumbent or {}).get("units")
    for _ in range(attempts):
        text, p, c = complete(msgs)
        out["tokens"]["prompt"] += p
        out["tokens"]["completion"] += c
        out["raw"].append(text)
        d = _json_obj(text)
        if d is None or not isinstance(d.get("table"), dict):
            probs = ["the answer is not one JSON object with a \"table\""]
        else:
            table = {u: {k: (int(v) if k == "repairs" and isinstance(v, (int, float)) and float(v).is_integer() else v)
                         for k, v in (row or {}).items()} for u, row in d["table"].items()}
            probs = validate_table(table, inc_units, unit_ids, floor_share=floor_share, max_change=max_change)
            if not probs:
                rules = [str(r)[:300] for r in (d.get("rules") or [])][:2]
                out.update(table=table, reason=str(d.get("reason") or ""), rules=rules)
                out["problems"].append([])
                return out
        out["problems"].append(probs)
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": "The table breaks these constraints; answer again with a corrected table:\n- "
                  + "\n- ".join(probs)}]
    return out


# ---------------------------------------------------------------------------------------------- the E1 trial

def run_stats(run_dir: Path, diagnostics: dict[str, Any]) -> dict[str, Any]:
    """One round's figures for the acceptance rule; waste = repairs that changed nothing + sessions without progress
    + unused tokens as a share of B."""
    rnd = json.loads((Path(run_dir) / "round.json").read_text(encoding="utf-8"))
    m, w = rnd["measures"], diagnostics["waste"]
    return {"submittable_ratio": float(m["submittable"]), "frozen_pass_rate": float(m["frozen_pass_rate"]),
            "gated_units": float(m["gated_units"]),
            "waste": w["ineffective_repairs"] + w["no_progress_sessions"] + w["unused_tokens"] / rnd["config"]["budget"]["tokens"]}


def mean_stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {k: sum(r[k] for r in runs) / len(runs) for k in ("submittable_ratio", "frozen_pass_rate", "gated_units", "waste")}


def better(run: dict[str, Any], incumbent_runs: list[dict[str, Any]], *, eps_noise: float) -> tuple[bool, str]:
    """One candidate round against the incumbent's mean, by the priority rule (D8)."""
    return accept(run, mean_stats(incumbent_runs), eps_noise=eps_noise)


def decide(candidate_runs: list[dict[str, Any]], incumbent_runs: list[dict[str, Any]], *, eps_noise: float) -> str:
    """'accept', 'reject' or 'more' (run a 3rd round)."""
    b = [better(r, incumbent_runs, eps_noise=eps_noise)[0] for r in candidate_runs]
    if len(b) < 2:
        return "more"
    if len(b) == 2:
        return "accept" if all(b) else "reject" if not any(b) else "more"
    return "accept" if sum(b) >= 2 else "reject"
