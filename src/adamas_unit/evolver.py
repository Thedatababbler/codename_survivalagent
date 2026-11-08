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
- ``run_stats`` / ``eps_of`` / ``decide``: the E1 trial of a candidate (user decision 2026-11-08): judged on the
  held-out feedback half's pass ratio (one number per round from the sealed program) after the submittable check;
  2 rounds, a 3rd when mixed. ``better`` (all of D8 on the frozen figures) and ``direction`` (the last unit got
  budget for its repairs and its failures fell) are recorded only.
- The evolver sees units as U1..Un only (``anonymize``); its table comes back in real ids.
- The table's approval parameters (``ledger.APPROVAL_DEFAULTS``) are evolvable too (2026-11-08); ``validate_approval``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from adamas_unit.ledger import APPROVAL_CHOICES, APPROVAL_DEFAULTS, UNIT_TOPUP_LIMIT_MAX

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


def validate_approval(approval: Any) -> list[str]:
    """Problems of a proposed approval block ([] = it passes; None / absent = the defaults)."""
    if approval is None:
        return []
    if not isinstance(approval, dict):
        return ["approval: not an object"]
    bad = [f"approval.{k}: unknown parameter" for k in approval if k not in APPROVAL_DEFAULTS]
    for k, choices in APPROVAL_CHOICES.items():
        if k in approval and approval[k] not in choices:
            bad.append(f"approval.{k}: {approval[k]!r} not one of {list(choices)}")
    lim = approval.get("unit_topup_limit")
    if lim is not None and (isinstance(lim, bool) or not isinstance(lim, (int, float)) or not 0 <= lim <= UNIT_TOPUP_LIMIT_MAX):
        bad.append(f"approval.unit_topup_limit: {lim!r} not null or a number in 0..{UNIT_TOPUP_LIMIT_MAX:g}")
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

The table may also set approval parameters (omitted ones keep their default; the defaults are how pi_0 runs):
- "grant_cap": "floors" (default): the mechanical cap on a top-up is what is left minus the floors of the later
  units; "later_table_shares": what is left minus the later units' table shares x B (a share below the floor counts
  as the floor).
- "unit_topup_limit": null (default): no limit per unit; a number x in 0..3: all top-ups of one unit together are at
  most x times its allocation at the start of the unit (0: no top-ups).
- "inherited": whether a unit whose own tests pass spends a repair on failures of earlier units' tests; "planner"
  (default): the planner decides each time; "never"; "fixed": yes while repairs are left.

Rounds are compared by, in this order: the submittable ratio (the final code imports, its documented names import,
the suites collect); then the pass ratio of the final code on a set of hidden tests ("hidden_pass_ratio" in the
records), where a difference must exceed the noise between repeated rounds. The records also carry process figures
(the frozen gate tests' pass count, units whose gate passed, waste); they do not decide.

You see only numbers from the ledger: no code, no tests. Propose the budget table for the next round. Constraints
(checked mechanically; a table that breaks one is returned to you once with the problems):
- every unit appears, with "tokens", "seconds", "compressions" (shares of B) and "repairs" (an integer 0..3);
- for each kind the shares sum to at most 1, and every share is at least the floor;
- every share moves at most the given fraction relative to the incumbent's share (pi_0's shares count as 1/n).

In "reason", say which numbers in the records lead to each change (shares and approval parameters). You may also state at most 2 general rules
(short sentences that would hold for other tasks); they are only recorded.

Answer with one JSON object and nothing else:
{"table": {"<U1..Un>": {"tokens": <share>, "seconds": <share>, "compressions": <share>, "repairs": <int>}, ...},
 "approval": {"grant_cap": ..., "unit_topup_limit": ..., "inherited": ...},
 "reason": "<text>", "rules": ["<rule>", ...]}"""


def _no_ids(x: Any) -> Any:
    """Drop strings that look like test node ids (never needed: the evolver sees counts)."""
    if isinstance(x, dict):
        return {k: _no_ids(v) for k, v in x.items() if not (isinstance(v, str) and "::" in v)}
    if isinstance(x, list):
        return [_no_ids(v) for v in x if not (isinstance(v, str) and "::" in v)]
    return x


def round_record(run_dir: Path, diagnostics: dict[str, Any], hidden_ratio: float | None = None) -> dict[str, Any]:
    """What the evolver reads of one finished round: numbers only (``hidden_ratio``: the held-out feedback half's
    pass ratio, the one number the sealed program returns)."""
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
                    "hidden_pass_ratio": hidden_ratio,
                    "measures": {**{k: v for k, v in rnd["measures"].items() if k != "frozen_pass_rate"},
                                 "note": "frozen_* and gated_units are process figures; they do not decide"},
                    "units": units, "topups": topups, "diagnostics": diag})


def anon_map(unit_ids: list[str]) -> dict[str, str]:
    """Real unit id -> U1..Un in execution order (2026-11-08: the evolver never sees a real unit name)."""
    return {u: f"U{i + 1}" for i, u in enumerate(unit_ids)}


def anonymize(text: str, unit_ids: list[str]) -> str:
    for u, a in sorted(anon_map(unit_ids).items(), key=lambda kv: -len(kv[0])):
        text = text.replace(u, a)
    return text


def _anon_table(units: dict[str, Any] | None, unit_ids: list[str]) -> dict[str, Any] | None:
    m = anon_map(unit_ids)
    return None if units is None else {m.get(u, u): v for u, v in units.items()}


def evolver_input(incumbent: dict[str, Any] | None, records: list[dict[str, Any]], unit_ids: list[str], *,
                  floor_share: float, max_change: float, trials: list[dict[str, Any]] | None = None) -> str:
    """``trials``: earlier candidate tables and how their trial ended (version, table, approval, decision)."""
    inc = {"name": "pi_0 (no table)", "table": None, "approval": APPROVAL_DEFAULTS} if not incumbent else \
        {"name": f"table v{incumbent.get('version')}", "table": _anon_table(incumbent.get("units"), unit_ids),
         "approval": {**APPROVAL_DEFAULTS, **(incumbent.get("approval") or {})}}
    trials = [{**t, "table": _anon_table(t.get("table"), unit_ids)} for t in trials or []]
    tr = f"Earlier candidate tables and their trials:\n{json.dumps(trials, indent=1, default=str)}\n\n" if trials else ""
    return anonymize(f"Units in order: {json.dumps(list(anon_map(unit_ids).values()))}\n"
            f"Floor share: {floor_share}; maximum move per share: {max_change:.0%} of the incumbent's share\n\n"
            f"Incumbent: {json.dumps(inc)}\n\n{tr}"
            f"Records of earlier rounds of this task (same B), oldest first (\"table_version\" 0 is pi_0):\n"
            f"{json.dumps(records, indent=1, default=str)}", unit_ids)


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
            max_change: float = 0.30, attempts: int = 2, trials: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """{"table" (None when no valid one), "approval" (None: defaults), "reason", "rules", "problems" per attempt,
    "tokens", "raw"}."""
    user = evolver_input(incumbent, records, unit_ids, floor_share=floor_share, max_change=max_change, trials=trials)
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    out: dict[str, Any] = {"table": None, "approval": None, "reason": "", "rules": [], "problems": [], "tokens": {"prompt": 0, "completion": 0},
                           "raw": []}
    inc_units = (incumbent or {}).get("units")
    for _ in range(attempts):
        text, p, c = complete(msgs)
        out["tokens"]["prompt"] += p
        out["tokens"]["completion"] += c
        out["raw"].append(text)
        d = _json_obj(text)
        back = {a: u for u, a in anon_map(unit_ids).items()}
        if d is None or not isinstance(d.get("table"), dict):
            probs = ["the answer is not one JSON object with a \"table\""]
        else:
            table = {back.get(u, u): {k: (int(v) if k == "repairs" and isinstance(v, (int, float)) and float(v).is_integer() else v)
                         for k, v in (row or {}).items()} for u, row in d["table"].items()}
            appr = d.get("approval")
            probs = validate_table(table, inc_units, unit_ids, floor_share=floor_share, max_change=max_change) \
                + validate_approval(appr)
            if not probs:
                rules = [str(r)[:300] for r in (d.get("rules") or [])][:2]
                appr = {**APPROVAL_DEFAULTS, **(appr or {})}
                out.update(table=table, approval=None if appr == APPROVAL_DEFAULTS else appr,
                           reason=str(d.get("reason") or ""), rules=rules)
                out["problems"].append([])
                return out
        out["problems"].append(probs)
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": anonymize("The table breaks these constraints; answer again with a corrected "
                                                       "table:\n- " + "\n- ".join(probs), unit_ids)}]
    return out


# ---------------------------------------------------------------------------------------------- the E1 trial

def run_stats(run_dir: Path, diagnostics: dict[str, Any], last_unit: str | None = None,
              hidden: float | None = None) -> dict[str, Any]:
    """One round's figures for the acceptance rule; waste = repairs that changed nothing + sessions without progress
    + unused tokens as a share of B."""
    rnd = json.loads((Path(run_dir) / "round.json").read_text(encoding="utf-8"))
    m, w = rnd["measures"], diagnostics["waste"]
    # the task's last unit (``last_unit``; default: the last one run): None figures when the round never reached it
    units = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((Path(run_dir) / "units").glob("*/unit.json"))]
    last = next((u for u in units if u["unit"] == last_unit), None) if last_unit else (units[-1] if units else None)
    last = last or {"gates": [], "unit": last_unit}
    empty = sum(1 for x in diagnostics.get("session_started_empty") or [] if x.get("unit") == last["unit"] and x.get("role") == "repairer")
    return {"hidden": hidden, "submittable_ratio": float(m["submittable"]), "frozen_pass_rate": float(m["frozen_pass_rate"]),
            "frozen_passed": int(m["frozen_passed"]), "gated_units": float(m["gated_units"]),
            "last_unit_failures": int(last["gates"][-1]["new"]) if last["gates"] else None,
            "last_unit_repairs_empty": empty if last["gates"] else None,
            "waste": w["ineffective_repairs"] + w["no_progress_sessions"] + w["unused_tokens"] / rnd["config"]["budget"]["tokens"]}


def mean_stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {k: sum(r[k] for r in runs) / len(runs) for k in ("submittable_ratio", "frozen_pass_rate", "gated_units", "waste")}


def better(run: dict[str, Any], incumbent_runs: list[dict[str, Any]], *, eps_noise: float) -> tuple[bool, str]:
    """One candidate round against the incumbent's mean, by the full priority rule (D8). E1 records it only."""
    return accept(run, mean_stats(incumbent_runs), eps_noise=eps_noise)


def eps_of(incumbent_ratios: list[float], floor: float = 0.01) -> float:
    """eps = max(half the range of the incumbent's feedback-half ratios, 0.01) (user decision 2026-11-08)."""
    return max((max(incumbent_ratios) - min(incumbent_ratios)) / 2, floor)


def decide(candidate_runs: list[dict[str, Any]], incumbent_runs: list[dict[str, Any]], *, eps: float) -> tuple[str, str]:
    """('accept' | 'reject' | 'more', why) by priorities 1-2 only (user decision 2026-11-08). Runs carry
    "submittable_ratio" and "hidden" (the feedback-half ratio).

    1. more rounds with submittable 0 than the incumbent had -> reject at once;
    2. two rounds: both above the incumbent's mean + eps -> accept; both not above the mean -> reject; otherwise a
       3rd round; three rounds: at least 2 above mean + eps -> accept, else reject."""
    zeros_inc = sum(1 for r in incumbent_runs if r["submittable_ratio"] == 0)
    zeros = sum(1 for r in candidate_runs if r["submittable_ratio"] == 0)
    if zeros > zeros_inc:
        return "reject", f"1: {zeros} rounds with submittable 0 > the incumbent's {zeros_inc}"
    mean = sum(r["hidden"] for r in incumbent_runs) / len(incumbent_runs)
    high = [r["hidden"] > mean + eps + 1e-12 for r in candidate_runs]
    low = [r["hidden"] <= mean + 1e-12 for r in candidate_runs]
    bar = f"incumbent mean {mean:.4f}, eps {eps:.4f}"
    if len(candidate_runs) < 2:
        return "more", f"fewer than 2 rounds ({bar})"
    if len(candidate_runs) == 2:
        if all(high):
            return "accept", f"2: both above mean + eps ({bar})"
        if all(low):
            return "reject", f"2: both not above the mean ({bar})"
        return "more", f"2: mixed ({bar})"
    n = sum(high)
    return ("accept" if n >= 2 else "reject"), f"2: {n} of {len(candidate_runs)} above mean + eps ({bar})"


def direction(candidate_runs: list[dict[str, Any]], incumbent_runs: list[dict[str, Any]]) -> dict[str, Any]:
    """'Direction correct' (recorded, never decides; user decision 2026-11-08): in the candidate's rounds the last
    unit's repair sessions got budget (none started below the margin) and its own remaining failures fell below the
    incumbent's mean. Not shown to the evolver."""
    def mean(rs: list[dict[str, Any]]) -> float | None:
        v = [r["last_unit_failures"] for r in rs]
        return None if not v or any(x is None for x in v) else sum(v) / len(v)
    inc, cand = mean(incumbent_runs), mean(candidate_runs)
    funded = bool(candidate_runs) and all(r["last_unit_repairs_empty"] == 0 for r in candidate_runs)
    return {"ok": funded and cand is not None and inc is not None and cand < inc, "last_unit_repairs_funded": funded,
            "last_unit_failures": cand, "incumbent_last_unit_failures": inc}
