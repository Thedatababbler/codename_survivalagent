"""The diagnostics (the eight of §8.4 and session_started_empty) of a finished round, computed from its ledger and unit records only (DESIGN §8.4, §8.5 step 1).

| diagnostic | rule here |
|---|---|
| exhausted_at | the unit in which the round budget ran out: used up, or less tokens left than one session's margin (round.json; recomputed from the ledger for rounds recorded before 2026-11-07) |
| session_started_empty | a session whose lease, with any top-up granted before its first call, was below the margin: it could not make a single call |
| truncated_in_progress | a session stopped by its budget (tokens, time, context) while its last probes' failures were falling |
| repairs_insufficient | a unit used all its repairs, did not pass, and its gate failures fell at every repair |
| repair_ineffective | a repair after which columns 1 + 3 did not change |
| unused_left | a unit closed with more than ``UNUSED_SHARE`` of its token allocation unused |
| degraded_after_compression | a probe right after a compression in the unit reports more failures than the one before it |
| context_stop | a session stopped on the context window with no compression left |
| no_progress_loop | a session reclaimed, or ``STALL`` consecutive probes without a code change or a change in failures |

Numbers only; no code, no test names. The output is what the evolver reads (with the table and round summaries).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

UNUSED_SHARE = 0.5
STALL = 3
BUDGET_STOPS = ("TokensExhausted", "TimeExceeded", "ContextExhausted", "RoundBudgetExhausted")


def _probes(events: list[dict], lease: str) -> list[dict]:
    return [e for e in events if e["event"] == "probe" and e.get("lease") == lease]


def diagnose(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    rnd = json.loads((run_dir / "round.json").read_text(encoding="utf-8"))
    events = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))["events"]
    units = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((run_dir / "units").glob("*/unit.json"))]
    d: dict[str, Any] = {"exhausted_at": rnd["measures"].get("exhausted_at"), "truncated_in_progress": [],
                         "repairs_insufficient": [], "repair_ineffective": [], "unused_left": [],
                         "degraded_after_compression": [], "context_stop": [], "no_progress_loop": []}
    closes = {e["unit"]: e for e in events if e["event"] == "close_unit"}
    margin = int(((rnd.get("config") or {}).get("agent_config") or {}).get("margin_tokens", 0))
    total = float(((rnd.get("config") or {}).get("budget") or {}).get("tokens", 0))
    unit_ids = [json.loads(p.read_text(encoding="utf-8"))["unit"] for p in sorted((run_dir / "units").glob("*/unit.json"))]
    if d["exhausted_at"] is None and margin and total:
        used = 0.0
        for k in sorted(closes):
            used += closes[k]["used"]["tokens"]
            if total - used < margin:
                d["exhausted_at"] = unit_ids[k] if k < len(unit_ids) else k
                break
    first_charge: dict[str, float] = {}
    for e in events:
        if e["event"] == "charge" and e.get("lease") not in first_charge:
            first_charge[e["lease"]] = e["t"]
    d["session_started_empty"] = []
    for e in events:
        if e["event"] != "lease" or not margin:
            continue
        lid = e["lease"]
        start = e["tokens"] + sum(g.get("granted", 0) for g in events if g["event"] == "grant" and g.get("lease") == lid
                                  and g["t"] < first_charge.get(lid, float("inf")))
        if start < margin:
            uid = unit_ids[e["unit"]] if e["unit"] < len(unit_ids) else e["unit"]
            d["session_started_empty"].append({"unit": uid, "role": e["role"], "lease": lid, "start_tokens": int(start)})
    for u in units:
        uid, k = u["unit"], u["index"]
        for s in u["sessions"]:
            st, lease = s.get("exit_status"), s.get("lease")
            if not lease:
                continue
            pr = _probes(events, lease)
            f = [p["failures"] for p in pr[-3:] if p.get("failures") is not None]
            if st in BUDGET_STOPS and len(f) >= 2 and f[-1] < f[0]:
                d["truncated_in_progress"].append({"unit": uid, "role": s["role"], "exit": st, "failures": f})
            if st == "ContextExhausted":
                d["context_stop"].append({"unit": uid, "role": s["role"]})
            stalled = any(all(p["lines_changed"] == 0 for p in pr[i:i + STALL]) and
                          len({p["failures"] for p in pr[i:i + STALL]}) == 1 for i in range(max(0, len(pr) - STALL + 1)))
            if st == "Reclaimed" or (len(pr) >= STALL and stalled):
                d["no_progress_loop"].append({"unit": uid, "role": s["role"], "exit": st, "probes": len(pr)})
        fails = [g["new"] + g["regression"] for g in u["gates"]]
        reps = u["repairs"]
        if reps and not u["passed"] and len(reps) >= u["allocation"]["repairs"] and all(b < a for a, b in zip(fails, fails[1:])):
            d["repairs_insufficient"].append({"unit": uid, "failures": fails})
        for r in reps:
            b, a = r["before"], r["after"]
            if b["new"] + b["regression"] == a["new"] + a["regression"] and (r["target"] == "own" or b["inherited"] == a["inherited"]):
                d["repair_ineffective"].append({"unit": uid, "repair": r["n"], "target": r["target"]})
        c = closes.get(k)
        if c and c["allocation"]["tokens"] > 0 and c["unused"]["tokens"] / c["allocation"]["tokens"] > UNUSED_SHARE:
            d["unused_left"].append({"unit": uid, "unused_tokens": int(c["unused"]["tokens"]),
                                     "share": round(c["unused"]["tokens"] / c["allocation"]["tokens"], 3)})
        comps = [e["t"] for e in events if e["event"] == "compression" and e["unit"] == k]
        for t in comps:
            leases = {s.get("lease") for s in u["sessions"]}
            pr = [e for e in events if e["event"] == "probe" and e.get("lease") in leases and e.get("failures") is not None]
            before = [p for p in pr if p["t"] < t]
            after = [p for p in pr if p["t"] > t]
            if before and after and after[0]["failures"] > before[-1]["failures"]:
                d["degraded_after_compression"].append({"unit": uid, "before": before[-1]["failures"], "after": after[0]["failures"]})
    d["counts"] = {k: (len(v) if isinstance(v, list) else int(v is not None)) for k, v in d.items()}
    d["measures"] = rnd["measures"]
    d["waste"] = {"unused_tokens": sum(x["unused_tokens"] for x in d["unused_left"]),
                  "ineffective_repairs": len(d["repair_ineffective"]), "no_progress_sessions": len(d["no_progress_loop"])}
    return d
