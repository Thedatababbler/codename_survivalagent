#!/usr/bin/env python3
"""Run ContextBench instances on the v0.3 executor. SPENDS QUOTA.

    set -a; . ./.env; set +a
    uv run python scripts/cb_run.py --round probe-1 --arm pi0 --tokens 3000000 --run kv_store:R1000:1001 [--run ...]

Each ``--run`` is ``family:level:seed``. Runs go one after the other; the batch stops at the first void run, at the
first run the grader could not grade from snapshots, and when ``--batch-tokens`` is used up. Prints one line of
figures per run and the breach section.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.model import agent_model, complete, load_env  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402
from survival import cb as CB  # noqa: E402
from survival import cb_run as RUN  # noqa: E402
from survival.skills import load_skillset  # noqa: E402

PI0 = ROOT / "configs" / "cb_skills_pi0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", required=True)
    ap.add_argument("--arm", choices=RUN.ARMS, required=True)
    ap.add_argument("--run", action="append", required=True, help="family:level:seed (repeatable)")
    ap.add_argument("--tokens", type=int, required=True, help="cap of one run (prompt tokens)")
    ap.add_argument("--batch-tokens", type=int, default=0, help="stop the batch when this many prompt tokens are used (0: no cap)")
    ap.add_argument("--seconds", type=float, default=21600)
    ap.add_argument("--skills", default=str(PI0))
    ap.add_argument("--skills-id", default="cb_pi0")
    ap.add_argument("--reps", type=int, default=1)
    a = ap.parse_args()
    load_env()
    require_sandbox("ContextBench run")
    CB.ensure()
    skills = None if a.arm == "none" else load_skillset(a.skills)
    jobs = []
    for spec in a.run:
        fam, lv, seed = spec.split(":")
        CB.instance(fam, lv, int(seed))                 # generated (and its seed and level checked) before anything is spent
        jobs += [(fam, lv, int(seed), rep) for rep in range(a.reps)]
    used, scans, stop = 0, [], ""
    for fam, lv, seed, rep in jobs:
        if a.batch_tokens and used >= a.batch_tokens:
            stop = f"batch cap reached ({used} of {a.batch_tokens})"
            break
        cap = min(a.tokens, a.batch_tokens - used) if a.batch_tokens else a.tokens    # a run never takes the batch past its cap
        r = RUN.run(fam, lv, seed, arm=a.arm, round_id=a.round, cfg=RUN.RunConfig(tokens=cap, seconds=a.seconds),
                    agent_model=agent_model, complete=complete, skills=skills, skills_id="" if a.arm == "none" else a.skills_id,
                    rep=rep)
        run_dir = RUN.OUT / a.round / r["run"]
        s = breach.scan_run(run_dir, run_id=f"cb-{a.round}-{r['run']}")
        scans.append(s)
        used += r["tokens"]["prompt"]
        line = {"run": r["run"], "family": fam, "level": lv, "seed": seed, "arm": a.arm, "rep": rep, "reward": r["reward"],
                "terminated_by": r["terminated_by"], "exit_status": r["exit_status"], "ops": f"{r['ops_delivered']}/{r['ops_total']}",
                "prompt_tokens": r["tokens"]["prompt"], "completion_tokens": r["tokens"]["completion"],
                "cached_tokens": r["tokens"]["cached"], "summary_prompt_tokens": r["tokens"]["summary_prompt"],
                "compliance": r["compliance"],
                **{k: r["counts"][k] for k in ("calls", "compactions", "rollbacks", "truncations", "reminders", "format_hints")},
                "peak_o200k": r["context"]["peak_o200k"], "served_per_o200k": r["context"]["served_per_o200k"],
                "graded_from": r["graded_from"], "void": s["void"], "warning": s["warning"], "critical": s["critical"],
                "wall_seconds": r["wall_seconds"]}
        print(json.dumps(line), flush=True)
        with (RUN.OUT / a.round / "runs.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        if s["void"]:
            stop = f"run {r['run']} is void"
            break
        if r["graded_from"] != "snapshots" or r["grader_valid"] != 1.0:
            stop = f"run {r['run']}: the grader did not grade from snapshots"
            break
        if r["exit_status"] in ("TokensExhausted", "RoundBudgetExhausted"):
            stop = f"run {r['run']} used its whole cap"
            break
    print(breach.report_section(scans))
    if stop:
        print("STOPPED: " + stop)
    return 1 if stop else 0


if __name__ == "__main__":
    raise SystemExit(main())
