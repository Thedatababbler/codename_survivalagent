#!/usr/bin/env python3
"""Run one v0.3 round. SPENDS QUOTA.

    uv run python scripts/v03_run_round.py --task nl2_python-pathspec --round v03-smoke-1 --arm pi0 --tokens 3000000

Writes outputs/rounds_v03/<round>/<task>/, scans it for breaches, scores the final code on the held-out feedback half
(the sealed program; one number) and prints the round's figures and the breach section.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.feedback import feedback_ratio  # noqa: E402
from adamas_unit.model import agent_model, complete, load_env, summarize  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402
from survival.ledger import Limits  # noqa: E402
from survival.note import digest  # noqa: E402
from survival.round import ARMS, Models, Round, RoundConfig  # noqa: E402
from survival.skills import load_skillset  # noqa: E402
from survival.tasks import forbidden_names, import_check, load_task  # noqa: E402

PI0 = ROOT / "memory" / "planner" / "skills"
OUT = ROOT / "outputs" / "rounds_v03"


def run_round(*, task_id: str, round_id: str, arm: str, tokens: int, skills_dir: Path = PI0, skills_id: str = "pi0",
              window: int = 64_000, seconds: float = 14400, score: bool = True, **limits) -> dict:
    load_env()
    require_sandbox("v0.3 round")
    task = load_task(task_id)
    skills = load_skillset(skills_dir, forbidden=forbidden_names())
    cfg = RoundConfig(limits=Limits(tokens=tokens, seconds=seconds, **limits), context_window=window)
    run_dir = OUT / round_id / task_id
    rnd = Round(task, run_dir, cfg, Models(agent=agent_model, complete=complete, summarize=summarize), skills, arm=arm,
                importcheck=import_check(task), skills_id=skills_id)
    rec = rnd.run()
    s = breach.scan_run(run_dir, run_id=f"{round_id}-{task_id}")
    ratio = None
    if score and not s["void"]:
        ratio = feedback_ratio(task_id, round_id, run_dir)
    out = {"round": round_id, "task": task_id, "arm": arm, "skills": skills_id, "usable": not s["void"], "void": s["void"],
           "ratio": ratio, "submittable": rec["measures"]["submittable"], "resources": rec["resources"], "counts": rec["counts"],
           "exit_status": rec["exit_status"], "digest": digest(rec, ratio),
           "importcheck": {k: v for k, v in (rec.get("importcheck") or {}).items() if k not in ("records", "final", "config")},
           "breach": {k: s[k] for k in ("commands", "warning", "critical", "void")}, "wall_seconds": rec["wall_seconds"]}
    (run_dir / "result.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    out["breach_section"] = breach.report_section([s])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--round", required=True, help="round id (a new directory)")
    ap.add_argument("--arm", choices=ARMS, default="pi0")
    ap.add_argument("--tokens", type=int, required=True, help="the round budget B (prompt tokens)")
    ap.add_argument("--seconds", type=float, default=14400)
    ap.add_argument("--window", type=int, default=64_000)
    ap.add_argument("--skills", default=str(PI0), help="a directory with the five skills (default: pi_0)")
    ap.add_argument("--skills-id", default="pi0")
    ap.add_argument("--no-score", action="store_true")
    a = ap.parse_args()
    out = run_round(task_id=a.task, round_id=a.round, arm=a.arm, tokens=a.tokens, seconds=a.seconds, window=a.window,
                    skills_dir=Path(a.skills), skills_id=a.skills_id, score=not a.no_score)
    section = out.pop("breach_section")
    print(section)
    print(json.dumps({k: v for k, v in out.items() if k != "digest"}, indent=1, default=str))
    return 1 if out["void"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
