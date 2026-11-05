#!/usr/bin/env python3
"""One round of one task (DESIGN §4-§7). SPENDS QUOTA. Positive control first (scripts/positive_control.py).

    uv run python scripts/run_round.py --task nl2_cerberus --set set1 --round e0-r1 \
        --tokens 20000000 --seconds 14400 --compressions 8 --probe-every 10

Repairs default to 2 x units. The budget table and rules come from the planner state (outputs/planner/<task>/;
none: pi_0, even split). Writes outputs/rounds/<round>/<task>/ and appends the round's summary (numbers only) to the
planner state; prints the breach section.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.ledger import Budget  # noqa: E402
from adamas_unit.model import agent_model, complete, summarize  # noqa: E402
from adamas_unit.planner_state import PlannerState  # noqa: E402
from adamas_unit.round import Models, Round, RoundConfig  # noqa: E402
from adamas_unit.task import load_task  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--set", default="set1", help="frozen suite set under outputs/suites/<task>/")
    ap.add_argument("--round", required=True, help="round id (a new directory)")
    ap.add_argument("--tokens", type=int, required=True)
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--compressions", type=int, default=8)
    ap.add_argument("--repairs", type=int, default=0, help="0: 2 x units")
    ap.add_argument("--probe-every", type=int, default=0)
    ap.add_argument("--fixed-planner", action="store_true", help="the planner's fixed rule only (no planner calls)")
    a = ap.parse_args()
    require_sandbox("round")
    task = load_task(a.task)
    state = PlannerState(a.task)
    cfg = RoundConfig(budget=Budget(tokens=a.tokens, seconds=a.seconds, compressions=a.compressions,
                                    repairs=a.repairs or 2 * len(task.units)), probe_every=a.probe_every)
    models = Models(agent=agent_model, complete=None if a.fixed_planner else complete, summarize=summarize)
    run_dir = ROOT / "outputs" / "rounds" / a.round / a.task
    rnd = Round(task, ROOT / "outputs" / "suites" / a.task / a.set, run_dir, cfg, models, table=state.table(),
                rules=state.rules())
    rec = rnd.run()
    s = breach.scan_run(run_dir, run_id=f"{a.round}-{a.task}")
    state.add_summary({"round": a.round, "table_version": rec["table_version"], "measures": rec["measures"],
                       "void": s["void"],
                       "units": [{"unit": u["unit"], "passed": u["passed"], "repairs": len(u["repairs"])} for u in rec["units"]],
                       "ledger": {k: rec["ledger"][k] for k in ("used", "left", "side")}})
    print(json.dumps({"measures": rec["measures"], "used": rec["ledger"]["used"], "void": s["void"]}, indent=1))
    print(breach.report_section([s]))
    return 1 if s["void"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
