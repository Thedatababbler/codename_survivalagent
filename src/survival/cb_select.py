"""Selection rules of the ContextBench setting that are not scores (docs/CONTEXTBENCH.md §9).

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
