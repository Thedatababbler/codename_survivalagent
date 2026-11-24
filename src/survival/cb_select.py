"""Selection among skill combinations in the ContextBench setting (docs/CONTEXTBENCH.md §9, §11).

Order: compliance first (below), then two tiers. Tier 1: a combination whose quality (the mean over tasks of the
task's mean score over its instances) is below the best by more than the noise is out (noise: the mean of the
tasks' eps from the calibration). Tier 2: among the rest, a combination that another one matches or beats on all
four resources (tokens, compactions, roll-backs, peak context) and beats on one, is out. If more than ``cap``
remain, those with the largest normalised resource sum go.

Compliance (user decision 2026-11-22): needle_retention and sudoku_sketchpad forbid writing to disk. A combination
whose runs on those two tasks wrote task content to disk more often than pi_0's runs did is discarded before any
comparison of scores. kv_store and log_triage allow the disk and are not part of the rate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from survival.cb_run import DISK_FORBIDDEN, ROLES


def violation_rate(results: list[dict[str, Any]]) -> float | None:
    """The share of the runs on the two disk-forbidding tasks that wrote task content to disk (None: no such run)."""
    runs = [r for r in results if r["family"] in DISK_FORBIDDEN]
    if not runs:
        return None
    return sum(1 for r in runs if r["compliance"]["violation"]) / len(runs)


def discarded_for_compliance(candidate: list[dict[str, Any]], pi0: list[dict[str, Any]]) -> bool:
    c, b = violation_rate(candidate), violation_rate(pi0)
    if c is None:
        return False
    return c > (b or 0.0)


def evolver_rules() -> str:
    """The part of the evolver's prompt that states the compliance rule."""
    return (Path(ROLES) / "evolver_rules.md").read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------------------------- two-tier frontier

RESOURCES = ("tokens", "compactions", "rollbacks", "peak_context")


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def score(runs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """``runs``: {"family:level:seed": [run figures (cb_evolve.run_figures), usable ones only]}. Runs of one
    instance are averaged first, then the instances of a task, then the tasks."""
    by_task: dict[str, list[dict[str, float]]] = {}
    flat = []
    for key, rs in runs.items():
        if not rs:
            continue
        flat += rs
        inst = {"reward": _mean([r["reward"] for r in rs]), **{k: _mean([float(r["resources"][k]) for r in rs]) for k in RESOURCES}}
        by_task.setdefault(key.split(":")[0], []).append(inst)
    per_task = {t: _mean([i["reward"] for i in xs]) for t, xs in by_task.items()}
    return {"quality": _mean(list(per_task.values())), "per_task": per_task,
            "resources": {k: _mean([_mean([i[k] for i in xs]) for xs in by_task.values()]) for k in RESOURCES},
            "violation_rate": violation_rate(flat), "results": flat, "runs": {k: len(rs) for k, rs in runs.items()}}


def dominates(a: dict[str, float], b: dict[str, float]) -> bool:
    return all(a[k] <= b[k] for k in RESOURCES) and any(a[k] < b[k] for k in RESOURCES)


def normalised_sum(res: dict[str, float], ref: dict[str, float]) -> float:
    return sum(res[k] / ref[k] if ref.get(k) else res[k] for k in RESOURCES)


def select(scores: dict[str, dict[str, Any]], *, eps: dict[str, float], baseline: str, cap: int = 8) -> dict[str, Any]:
    out: dict[str, str] = {}
    base = scores[baseline]
    alive = {}
    for k, s in scores.items():
        if k != baseline and discarded_for_compliance(s["results"], base["results"]):
            out[k] = (f"compliance: broke the task instructions in {s['violation_rate']:.2f} of its runs on the "
                      f"disk-forbidding tasks, the baseline in {(base['violation_rate'] or 0.0):.2f}")
        else:
            alive[k] = s
    e = _mean(list(eps.values())) if eps else 0.01
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


def final_pick(scores: dict[str, dict[str, Any]], frontier: list[str], baseline: str) -> str:
    """The highest quality on the frontier (step E tests this one); exact ties go to the cheapest."""
    ref = scores[baseline]["resources"]
    return min(frontier, key=lambda k: (-round(scores[k]["quality"], 9), normalised_sum(scores[k]["resources"], ref)))
