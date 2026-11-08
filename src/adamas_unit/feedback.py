"""The held-out feedback half's pass ratio of a round's final code (user decision 2026-11-08; DESIGN §8.3).

Computed only by the sealed program ``scripts/sealed/heldout_half.py`` (which alone opens the held-out suite and
prints one number per repository); this module runs it and caches that number per round in
``outputs/feedback/<task>/<round>.json`` (the ratio and nothing else). The evaluation half is never computed here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "outputs" / "feedback"
SCRIPT = ROOT / "scripts" / "sealed" / "heldout_half.py"


def cached(task: str, round_id: str) -> float | None:
    p = CACHE / task / f"{round_id}.json"
    return json.loads(p.read_text())["feedback_ratio"] if p.is_file() else None


def feedback_ratio(task: str, round_id: str, run_dir: Path) -> float:
    """The cached ratio, else the sealed program's (and cache it)."""
    v = cached(task, round_id)
    if v is not None:
        return v
    final = Path(run_dir) / "final"
    if not final.is_dir():
        raise FileNotFoundError(f"{final}: no final code")
    p = subprocess.run([sys.executable, str(SCRIPT), "score", task, str(final)], capture_output=True, text=True,
                       cwd=ROOT, timeout=3600, check=True)
    r = json.loads(p.stdout.strip().splitlines()[-1])["ratios"][0]
    if r is None:
        raise RuntimeError(f"{round_id}: the sealed program returned no ratio")
    (CACHE / task).mkdir(parents=True, exist_ok=True)
    (CACHE / task / f"{round_id}.json").write_text(json.dumps({"round": round_id, "feedback_ratio": r}), encoding="utf-8")
    return float(r)
