#!/usr/bin/env python3
"""Run one v0.3 round. SPENDS QUOTA.

    uv run python scripts/v03_run_round.py --task nl2_python-pathspec --round v03-smoke-1 --arm pi0 --tokens 3000000

Writes outputs/rounds_v03/<round>/<task>/, scans it for breaches, scores the final code on the held-out feedback half
(the sealed program; one number) and prints the round's figures and the breach section.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.feedback import feedback_ratio  # noqa: E402
from adamas_unit.model import agent_model, complete, load_env, summarize  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import SCORING_AUDIT_LOG, require_sandbox  # noqa: E402
from survival.ledger import Limits  # noqa: E402
from survival.note import digest  # noqa: E402
from survival.round import ARMS, Models, Round, RoundConfig  # noqa: E402
from survival.skills import load_skillset  # noqa: E402
from survival.tasks import forbidden_names, import_check, load_task  # noqa: E402

PI0 = ROOT / "memory" / "planner" / "skills"
OUT = ROOT / "outputs" / "rounds_v03"


def run_round(*, task_id: str, round_id: str, arm: str, tokens: int, skills_dir: Path = PI0, skills_id: str = "pi0",
              window: int = 64_000, seconds: float = 14400, score: bool = True, out_root: Path = OUT,
              first_stage_only: bool = False, transfer_final: bool = False, **limits) -> dict:
    load_env()
    require_sandbox("v0.3 round")
    import yaml
    frozen = (yaml.safe_load((ROOT / "configs" / "v03_transfer_tasks.yaml").read_text()) or {}).get("transfer") or []
    if task_id in frozen and not transfer_final:
        raise SystemExit(f"{task_id} is a frozen transfer-test task: it is not run during evolution (--transfer-final for the final comparison)")
    task = load_task(task_id)
    ic = import_check(task)
    if first_stage_only:                 # harness tests only: one stage, no closing import check, never compared
        task = dataclasses.replace(task, units=task.units[:1])
        ic, score = None, False
    skills = load_skillset(skills_dir, forbidden=forbidden_names())
    cfg = RoundConfig(limits=Limits(tokens=tokens, seconds=seconds, **limits), context_window=window)
    run_dir = Path(out_root) / round_id / task_id
    rnd = Round(task, run_dir, cfg, Models(agent=agent_model, complete=complete, summarize=summarize), skills, arm=arm,
                importcheck=ic, skills_id=skills_id)
    rec = rnd.run()
    s = breach.scan_run(run_dir, run_id=f"{round_id}-{task_id}")
    ratio = None
    audit: dict = {}
    if score and not s["void"]:
        n0 = len(SCORING_AUDIT_LOG.read_text().splitlines()) if SCORING_AUDIT_LOG.is_file() else 0
        ratio = feedback_ratio(task_id, round_id, run_dir)
        # what the scored code tried during sealed scoring (the scoring user, file rules on: all of it was refused)
        new = SCORING_AUDIT_LOG.read_text().splitlines()[n0:] if SCORING_AUDIT_LOG.is_file() else []
        mine = [e for e in map(json.loads, new) if e.get("cwd", "").endswith("/" + task_id)]
        audit = {"warning": sum(e["level"] == "WARNING" for e in mine), "critical": sum(e["level"] == "CRITICAL" for e in mine),
                 "critical_events": [e for e in mine if e["level"] == "CRITICAL"][:20]}
    out = {"round": round_id, "task": task_id, "arm": arm, "skills": skills_id, "usable": not s["void"], "void": s["void"],
           "ratio": ratio, "submittable": rec["measures"]["submittable"], "resources": rec["resources"], "counts": rec["counts"],
           "exit_status": rec["exit_status"], "digest": digest(rec, ratio),
           "importcheck": {k: v for k, v in (rec.get("importcheck") or {}).items() if k not in ("records", "final", "config")},
           "breach": {k: s[k] for k in ("commands", "warning", "critical", "void")}, "scoring_audit": audit, "wall_seconds": rec["wall_seconds"]}
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
    ap.add_argument("--skills", default=str(PI0), help="a directory with the six skills (default: pi_0)")
    ap.add_argument("--skills-id", default="pi0")
    ap.add_argument("--no-score", action="store_true")
    ap.add_argument("--out-root", default=str(OUT))
    ap.add_argument("--first-stage-only", action="store_true", help="harness test: only the plan's first stage")
    ap.add_argument("--transfer-final", action="store_true", help="the final comparison on a frozen transfer-test task")
    ap.add_argument("--reviews", type=int, default=None, help="harness test / probe: the review-call limit")
    ap.add_argument("--subagents", type=int, default=None, help="harness test / probe: the limit of all sub-agent calls")
    ap.add_argument("--compressions", type=int, default=None, help="harness test: the compaction pool")
    a = ap.parse_args()
    out = run_round(task_id=a.task, round_id=a.round, arm=a.arm, tokens=a.tokens, seconds=a.seconds, window=a.window,
                    skills_dir=Path(a.skills), skills_id=a.skills_id, score=not a.no_score, out_root=Path(a.out_root),
                    first_stage_only=a.first_stage_only, transfer_final=a.transfer_final,
                    **{k: getattr(a, k) for k in ("compressions", "reviews", "subagents") if getattr(a, k) is not None})
    section = out.pop("breach_section")
    print(section)
    print(json.dumps({k: v for k, v in out.items() if k != "digest"}, indent=1, default=str))
    return 1 if out["void"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
