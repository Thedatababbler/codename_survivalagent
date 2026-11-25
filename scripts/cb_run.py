#!/usr/bin/env python3
"""Run ContextBench instances on the v0.3 executor. SPENDS QUOTA.

    set -a; . ./.env; set +a
    uv run python scripts/cb_run.py --round probe-1 --arm pi0 --tokens 3000000 --run kv_store:R1000:1001 [--run ...]

Each ``--run`` is ``family:level:seed``. Runs go one after the other; the batch stops at the first void run, at the
first run the grader could not grade from snapshots, and when ``--batch-tokens`` is used up. Prints one line of
figures per run and the breach section.

``--parallel N`` (N > 1): every run is a process of its own, at most N at a time. The batch cap holds strictly: a
run starts only with a cap that is still free (``--batch-tokens`` minus what finished runs used minus the caps of
the runs in progress), and never with less than ``--min-cap``; a finished run frees what it did not use. After a
stop condition no new run starts; the runs in progress finish.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
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
    ap.add_argument("--parallel", type=int, default=1, help="runs at a time (each a process of its own)")
    ap.add_argument("--min-cap", type=int, default=5_000_000, help="with --parallel: no run starts with a smaller cap than this")
    ap.add_argument("--rep-offset", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    load_env()
    require_sandbox("ContextBench run")
    CB.ensure()
    skills = None if a.arm == "none" else load_skillset(a.skills)
    for spec in a.run:
        fam, lv, seed = spec.split(":")
        CB.instance(fam, lv, int(seed))                 # generated (and its seed and level checked) before anything is spent
    jobs = build_jobs(a.run, a.reps, a.rep_offset)
    if a.parallel > 1:
        return parallel(a, jobs)
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
    if not a.child:
        print(breach.report_section(scans))
    if stop:
        print("STOPPED: " + stop)
    return 1 if stop else 0


def build_jobs(specs: list[str], reps: int = 1, offset: int = 0) -> list[tuple[str, str, int, int]]:
    """(family, level, seed, rep) per run. Every repeat of an instance has a rep number of its own, whether it comes
    from ``--reps`` or from the same ``--run`` given more than once (2026-11-25: before, the latter were all rep 0)."""
    n: dict[tuple[str, str, int], int] = {}
    jobs = []
    for spec in specs:
        fam, lv, seed = spec.split(":")
        key = (fam, lv, int(seed))
        for _ in range(reps):
            jobs.append((*key, offset + n.get(key, 0)))
            n[key] = n.get(key, 0) + 1
    return jobs


def parallel(a, jobs) -> int:
    rdir = RUN.OUT / a.round
    (rdir / "logs").mkdir(parents=True, exist_ok=True)
    runs_file = rdir / "runs.jsonl"
    seen = len(runs_file.read_text().splitlines()) if runs_file.exists() else 0
    batch = a.batch_tokens or 10**15
    queue, running, used, stop, n_done = list(jobs), [], 0, "", 0
    while queue or running:
        for job in list(running):
            code = job["proc"].poll()
            if code is None:
                continue
            running.remove(job)
            job["log"].close()
            n_done += 1
            fam, lv, seed, rep = job["spec"]
            lines = [json.loads(x) for x in runs_file.read_text().splitlines()[seen:]] if runs_file.exists() else []
            mine = [x for x in lines if (x["family"], x["level"], x["seed"], x["rep"]) == (fam, lv, seed, rep)]
            if mine:
                used += mine[-1]["prompt_tokens"]
                print(json.dumps(mine[-1]), flush=True)
            else:                                       # no figures: its whole cap counts as used
                used += job["cap"]
                stop = stop or f"{fam}:{lv} rep {rep} ended without a result (exit code {code}); see {job['log'].name}"
            if code != 0:
                stop = stop or f"{fam}:{lv} rep {rep} stopped (exit code {code}); see {job['log'].name}"
        while queue and not stop and len(running) < a.parallel:
            free = batch - used - sum(j["cap"] for j in running)
            cap = min(a.tokens, free)
            if cap < min(a.min_cap, a.tokens):
                if not running:
                    stop = f"batch cap reached ({used} of {a.batch_tokens})"
                break
            fam, lv, seed, rep = queue.pop(0)
            log = (rdir / "logs" / f"{len(jobs) - len(queue):02d}-{fam}-{lv}-rep{rep}.log").open("w", encoding="utf-8")
            cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--round", a.round, "--arm", a.arm,
                   "--run", f"{fam}:{lv}:{seed}", "--tokens", str(cap), "--seconds", str(a.seconds), "--skills", a.skills,
                   "--skills-id", a.skills_id, "--rep-offset", str(rep)]
            running.append({"proc": subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT)),
                            "spec": (fam, lv, seed, rep), "cap": cap, "log": log})
            print(f"started {fam}:{lv} rep {rep} with cap {cap} (pid {running[-1]['proc'].pid})", flush=True)
        if stop and not running:
            break
        time.sleep(5)
    lines = [json.loads(x) for x in runs_file.read_text().splitlines()[seen:]] if runs_file.exists() else []
    scans = [breach.scan_run(rdir / x["run"], run_id=f"cb-{a.round}-{x['run']}", write=False) for x in lines]
    print(f"batch: {len(lines)} of {len(jobs)} runs with figures, {used} prompt tokens counted")
    print(breach.report_section(scans))
    if queue and not stop:
        stop = "runs left in the queue"
    if stop:
        print("STOPPED: " + stop)
    return 1 if stop else 0


if __name__ == "__main__":
    raise SystemExit(main())
