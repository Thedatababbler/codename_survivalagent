"""What the evolver reads of the runs (refactor §6.2): numbers only.

``digest`` turns a round's record into resource figures: per stage how much of its allocation was used, how it
ended, the sub-agent calls with their quotas as shares, the planner's actions with the readout at the moment they
fired, the context peaks, compactions, roll-backs and handoffs; plus the held-out feedback half's pass ratio (one
number). No stage name, no file, no command, no code, no test, nothing of the held-out suite.

``build_note`` is the evolver's evidence for one skill combination: a table of its runs and, for each task,
the better run against the worse run of the same combination (after the contrastive note of the Context Language
Models reference implementation: a proposer must see what went well next to what went badly).
"""

from __future__ import annotations

from typing import Any

RESOURCES = ("tokens", "repairs", "compressions", "peak_context")


def digest(rec: dict[str, Any], feedback_ratio: float | None) -> dict[str, Any]:
    lim = rec["config"]["limits"]
    B, window = lim["tokens"], rec["config"]["context_window"]
    stages = []
    subs = rec.get("subagents") or []
    fired = (rec.get("planner") or {}).get("fired") or []
    decisions = (rec.get("planner") or {}).get("decisions") or []
    for s in rec["stages"]:
        k = s["stage"]
        alloc = max(1, s["allocation"])
        stages.append({
            "stage": k + 1,
            "allocation_share_of_round": round(s["allocation"] / B, 3),
            "used_share_of_allocation": round(s["used"] / alloc, 3),
            "ended": "declared" if s["declared_done"] else "closed by the program",
            "below_minimum": bool(s["below_minimum"]),
            "documented_names_missing_at_end": s.get("missing"),
            "own_tests_passing_at_end": s.get("tests_passed"),
            "subagent_calls": [{"type": x["kind"], "quota_share_of_stage": round(x["quota"] / alloc, 3),
                                "used_share_of_quota": round(x["used"] / max(1, x["quota"]), 3),
                                "ended": x.get("exit_status"), "quota_was_cut": bool(x.get("cut"))}
                               for x in subs if x.get("stage") == k],
            "topups": [{"share_of_cap_granted": d["share_of_cap"], "granted_share_of_round": round(d["granted"] / B, 3)}
                       for d in decisions if d.get("stage") == k and d.get("kind") == "topup"],
            "planner_actions": [{"skill": f["skill"], "action": f["action"], **({"outcome": f["outcome"]} if "outcome" in f else {}),
                                 **f["state"]} for f in fired if f.get("stage") == k],
        })
    sessions = rec.get("sessions") or []
    ex = sessions[0] if sessions else {}
    r = rec["resources"]
    return {
        "arm": rec["arm"], "ended": rec.get("exit_status"), "submittable": rec["measures"]["submittable"],
        "hidden_pass_ratio": feedback_ratio,
        "resources": {"tokens_share_of_round": round(r["tokens"] / B, 3), "repairs": r["repairs"],
                      "compressions": r["compressions"], "peak_context_share_of_window": round(r["peak_context"] / window, 3)},
        "counts": rec["counts"],
        "executor": {"model_calls": ex.get("calls"), "counted_steps": ex.get("steps"), "management_turns": ex.get("free_turns"),
                     "context_peak_share_of_window": round((ex.get("context") or {}).get("peak", 0) / window, 3),
                     "events": [{k: v for k, v in e.items() if k in ("event", "why", "from_memory", "skill", "action", "dropped", "outcome")}
                                for e in ex.get("events") or [] if e.get("event") != "probe"][:60]},
        "stages": stages,
    }


def resource_vector(rec: dict[str, Any]) -> dict[str, float]:
    return {k: float(rec["resources"][k]) for k in RESOURCES}


def _cost(d: dict[str, Any]) -> float:
    r = d["resources"]
    return r["tokens_share_of_round"] + 0.05 * r["repairs"] + 0.05 * r["compressions"] + r["peak_context_share_of_window"]


def contrast_pairs(runs: dict[str, list[dict[str, Any]]]) -> list[tuple[str, dict, dict, str]]:
    """(task label, worse, better, kind) for each task with two runs of the same combination."""
    out = []
    for label, ds in runs.items():
        ds = [d for d in ds if d.get("hidden_pass_ratio") is not None]
        if len(ds) < 2:
            continue
        s = sorted(ds, key=lambda d: (d["submittable"], d["hidden_pass_ratio"], -_cost(d)))
        lo, hi = s[0], s[-1]
        if (hi["submittable"], hi["hidden_pass_ratio"]) != (lo["submittable"], lo["hidden_pass_ratio"]):
            out.append((label, lo, hi, "quality"))
        elif _cost(lo) != _cost(hi):
            lo, hi = sorted(ds, key=_cost, reverse=True)[0], sorted(ds, key=_cost)[0]
            out.append((label, lo, hi, "resources (same quality)"))
    return out


def build_note(runs: dict[str, list[dict[str, Any]]]) -> str:
    """``runs``: {task label (T1, T2, ...; never a task's name): [digest, ...]} of one combination."""
    import json

    lines = ["# Runs of the current skill combination", "",
             "| task | run | hidden pass ratio | submittable | tokens (share of round) | repairs | compressions | "
             "peak context (share of window) | sub-agents | roll-backs | handoffs | stages closed by the program |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    ids: dict[int, str] = {}
    for label, ds in runs.items():
        for i, d in enumerate(ds, 1):
            ids[id(d)] = f"{label}-R{i}"
            r, c = d["resources"], d["counts"]
            lines.append(f"| {label} | {ids[id(d)]} | {d['hidden_pass_ratio']} | {d['submittable']} | {r['tokens_share_of_round']} | "
                         f"{r['repairs']} | {r['compressions']} | {r['peak_context_share_of_window']} | {c['subagents']} | "
                         f"{c['rollbacks']} | {c['handoffs']} | {c['stages_closed_by_program']} |")
    pairs = contrast_pairs(runs)
    lines += ["", f"## Better against worse runs of the same combination on the same task (n={len(pairs)})", ""]
    for label, lo, hi, kind in pairs:
        lines += [f"### {label}: worse {ids[id(lo)]} vs better {ids[id(hi)]} ({kind})", "",
                  f"worse {ids[id(lo)]}:", "```json", json.dumps(lo, indent=1), "```",
                  f"better {ids[id(hi)]}:", "```json", json.dumps(hi, indent=1), "```", ""]
    shown = {id(x) for _, lo, hi, _ in pairs for x in (lo, hi)}
    rest = [(label, d) for label, ds in runs.items() for d in ds if id(d) not in shown]
    if rest:
        lines += ["## The other runs", ""]
        for label, d in rest:
            lines += [f"{ids[id(d)]}:", "```json", json.dumps(d, indent=1), "```", ""]
    return "\n".join(lines) + "\n"
