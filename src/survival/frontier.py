"""Selection among skill combinations: a two-tier Pareto frontier (refactor §6.3, §6.5).

A combination's score, from its runs on the evolution tasks (at least two per task):

- ``submittable``: the share of its runs that are submittable;
- ``quality``: the mean over tasks of the task's mean held-out feedback ratio;
- four resources, each the mean over tasks of the task's mean: tokens, repair calls, compactions, peak context.

Selection: a combination less submittable than pi_0 is out. Tier 1: a combination whose quality is below the best
by more than the noise is out (noise: the mean of the tasks' eps, each measured on pi_0's baseline rounds). Tier
2: among the rest, a combination that another one matches or beats on all four resources, and beats on one, is
out. If more than ``cap`` remain, those with the largest normalised resource sum go.
"""

from __future__ import annotations

import math
from typing import Any

RESOURCES = ("tokens", "repairs", "compressions", "peak_context")


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def score(runs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """``runs``: {task: [{"ratio": float, "submittable": 0|1, "resources": {...}}, ...]} (usable runs only)."""
    per_task = {t: _mean([r["ratio"] for r in rs]) for t, rs in runs.items() if rs}
    allr = [r for rs in runs.values() for r in rs]
    return {"quality": _mean(list(per_task.values())), "per_task": per_task,
            "submittable": _mean([float(r["submittable"]) for r in allr]),
            "resources": {k: _mean([_mean([float(r["resources"][k]) for r in rs]) for rs in runs.values() if rs]) for k in RESOURCES},
            "runs": {t: len(rs) for t, rs in runs.items()}}


def noise(eps: dict[str, float]) -> float:
    return _mean(list(eps.values())) if eps else 0.01


def dominates(a: dict[str, float], b: dict[str, float]) -> bool:
    return all(a[k] <= b[k] for k in RESOURCES) and any(a[k] < b[k] for k in RESOURCES)


def normalised_sum(res: dict[str, float], ref: dict[str, float]) -> float:
    return sum(res[k] / ref[k] if ref.get(k) else res[k] for k in RESOURCES)


def select(scores: dict[str, dict[str, Any]], *, eps: dict[str, float], baseline: str, cap: int = 8) -> dict[str, Any]:
    """The frontier of ``scores`` ({combination id: score}); ``baseline`` is pi_0's id (it need not stay on it)."""
    out: dict[str, str] = {}
    base = scores[baseline]
    alive = {}
    for k, s in scores.items():
        if s["submittable"] + 1e-9 < base["submittable"]:
            out[k] = f"submittable {s['submittable']:.3f} below pi_0's {base['submittable']:.3f}"
        else:
            alive[k] = s
    e = noise(eps)
    best = max(s["quality"] for s in alive.values())
    for k in [k for k, s in alive.items() if best - s["quality"] > e]:
        out[k] = f"quality {alive[k]['quality']:.4f} below the best {best:.4f} by more than the noise {e:.4f}"
        del alive[k]
    for k in list(alive):
        by = next((j for j in alive if j != k and dominates(alive[j]["resources"], alive[k]["resources"])), None)
        if by is not None:
            out[k] = f"same quality within the noise, dominated on the four resources by {by}"
    alive = {k: s for k, s in alive.items() if k not in out}
    ref = base["resources"]
    while len(alive) > cap:
        worst = max(alive, key=lambda k: normalised_sum(alive[k]["resources"], ref))
        out[worst] = f"frontier over {cap}: the largest normalised resource sum"
        del alive[worst]
    return {"frontier": sorted(alive), "eliminated": out, "noise": e, "best_quality": best}


def paired_se(a: dict[str, float], b: dict[str, float]) -> float:
    d = [a[t] - b[t] for t in a if t in b]
    n = len(d)
    if n < 2:
        return 0.0
    m = sum(d) / n
    return math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1) / n)


def final_pick(scores: dict[str, dict[str, Any]], frontier: list[str], baseline: str) -> str:
    """The highest quality on the frontier; those within one standard error of it tie, and the cheapest wins."""
    best = max(frontier, key=lambda k: scores[k]["quality"])
    tied = [k for k in frontier
            if scores[best]["quality"] - scores[k]["quality"] <= paired_se(scores[best]["per_task"], scores[k]["per_task"]) + 1e-9]
    ref = scores[baseline]["resources"]
    return min(tied, key=lambda k: (normalised_sum(scores[k]["resources"], ref), -scores[k]["quality"]))
