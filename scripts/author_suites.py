#!/usr/bin/env python3
"""The authoring phase for one task (DESIGN §4.2): one frozen suite per unit, before all rounds. SPENDS QUOTA.

    uv run python scripts/author_suites.py --task nl2_cerberus --set set1 --tokens 4000000 --seconds 14400

Writes outputs/suites/<task>/<set>/ (frozen, with MANIFEST.json) and <set>.authoring/<task>/ (sessions, authoring ledger),
then the breach section of every authoring session.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.authoring import author_all  # noqa: E402
from adamas_unit.ledger import Budget  # noqa: E402
from adamas_unit.model import agent_model, summarize  # noqa: E402
from adamas_unit.task import load_task  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--set", default="set1")
    ap.add_argument("--tokens", type=int, required=True, help="authoring budget, prompt tokens (all units)")
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--fix-rounds", type=int, default=2)
    a = ap.parse_args()
    require_sandbox("authoring phase")
    task = load_task(a.task)
    sset = ROOT / "outputs" / "suites" / a.task / a.set
    m = author_all(task, sset, models=agent_model, budget=Budget(tokens=a.tokens, seconds=a.seconds),
                   fix_rounds=a.fix_rounds, summarizer=summarize,
                   agent_config={"margin_tokens": 60_000, "context_window": 200_000, "keep_recent": 6})
    work = sset.parent / f"{sset.name}.authoring" / a.task
    s = breach.scan_run(work, run_id=f"author-{a.task}-{a.set}")
    print(json.dumps({"manifest": m, "breach": {k: s[k] for k in ("commands", "warning", "critical", "void")}}, indent=1))
    print(breach.report_section([s]))
    return 1 if s["void"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
