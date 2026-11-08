#!/usr/bin/env python3
"""E1: the budget evolver on one task at one B (DESIGN §13; user rules 2026-11-07). SPENDS QUOTA.

    uv run python scripts/e1.py --task nl2_deepdiff --set adamas2 --tokens 1586206 \
        --baseline tier50-pi0-r3,tier50-pi0-r4,tier50-pi0-r5 --iterations 5

Per iteration: the evolver reads every valid round so far (the baseline and all candidate rounds, numbers only) and
proposes a table; the table is stored as a new version (never current while under trial) and run in 2 rounds side
by side; both better than the incumbent -> accepted (becomes current); neither -> rejected; one -> a 3rd round,
accepted when 2 of 3 are better. A void round is run again once. Every iteration writes
outputs/e1/<task>/iter_<n>.json: the proposal (table, reason, rules, constraint problems, tokens), its share changes,
the rounds, the decision. The evolver's tokens go on the evolution account (iteration files), not on any round's B.
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

from adamas_unit.diagnostics import diagnose  # noqa: E402
from adamas_unit.evolver import better, decide, mean_stats, propose, round_record, run_stats  # noqa: E402
from adamas_unit.model import complete  # noqa: E402
from adamas_unit.planner_state import PlannerState  # noqa: E402
from adamas_unit.task import load_task  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402


def rdir(task: str, rnd: str) -> Path:
    return ROOT / "outputs" / "rounds" / rnd / task


def invalid_rounds() -> set[str]:
    p = ROOT / "outputs" / "rounds" / "INVALID.jsonl"
    return {json.loads(x)["round"] for x in p.read_text().splitlines() if x.strip()} if p.is_file() else set()


def usable(task: str, rnd: str) -> bool:
    d = rdir(task, rnd)
    return (d / "round.json").is_file() and not breach.is_void(d) and rnd not in invalid_rounds()


def run_rounds(task: str, sset: str, tokens: int, version: int, names: list[str]) -> None:
    procs = []
    for n in names:
        log = (ROOT / "outputs" / "logs" / f"{n}.log").open("w")
        procs.append(subprocess.Popen([sys.executable, str(ROOT / "scripts" / "run_round.py"), "--task", task, "--set", sset,
                                       "--round", n, "--tokens", str(tokens), "--seconds", "14400", "--probe-every", "10",
                                       "--table-version", str(version)], stdout=log, stderr=subprocess.STDOUT,
                                      cwd=ROOT, start_new_session=True))
    for p in procs:
        p.wait()


def share_changes(table: dict, incumbent: dict | None, unit_ids: list[str]) -> dict:
    inc = (incumbent or {}).get("units") or {u: {"tokens": 1 / len(unit_ids), "seconds": 1 / len(unit_ids),
                                                  "compressions": 1 / len(unit_ids), "repairs": 3} for u in unit_ids}
    return {u: {k: [round(float(inc[u].get(k, 0)), 4), round(float(table[u][k]), 4)] for k in ("tokens", "seconds", "compressions", "repairs")
                if float(inc[u].get(k, 0)) != float(table[u][k])} for u in unit_ids}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--set", required=True)
    ap.add_argument("--tokens", type=int, required=True, help="the round budget B (tokens)")
    ap.add_argument("--baseline", required=True, help="comma-separated pi_0 rounds at this B")
    ap.add_argument("--iterations", type=int, default=5)
    ap.add_argument("--eps", type=float, default=-1, help="eps_noise on the pass rate; default: half the baseline's range")
    a = ap.parse_args()
    require_sandbox("E1")
    task = load_task(a.task)
    unit_ids = [u.unit_id for u in task.units]
    state = PlannerState(a.task)
    out = ROOT / "outputs" / "e1" / a.task
    out.mkdir(parents=True, exist_ok=True)
    stf = out / "e1_state.json"
    if stf.is_file():
        st = json.loads(stf.read_text())
    else:
        base = [r for r in a.baseline.split(",") if usable(a.task, r)]
        rates = [json.loads((rdir(a.task, r) / "round.json").read_text())["measures"]["frozen_pass_rate"] for r in base]
        eps = a.eps if a.eps >= 0 else (max(rates) - min(rates)) / 2
        st = {"task": a.task, "B_tokens": a.tokens, "eps_noise": eps, "rounds": base,
              "incumbent": {"name": "pi_0", "version": 0, "rounds": base}, "iterations": []}
    stf.write_text(json.dumps(st, indent=1))
    while len(st["iterations"]) < a.iterations:
        i = len(st["iterations"]) + 1
        records = [round_record(rdir(a.task, r), diagnose(rdir(a.task, r))) for r in st["rounds"] if usable(a.task, r)]
        inc_v = st["incumbent"]["version"]
        inc_table = state.table_version(inc_v) if inc_v else None
        prop = propose(complete, inc_table, records, unit_ids)
        it = {"iteration": i, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "incumbent": st["incumbent"]["name"],
              "proposal": {k: prop[k] for k in ("table", "reason", "rules", "problems", "tokens")}, "rounds": [], "decision": None}
        if prop["table"] is None:
            it["decision"] = "no valid proposal"
        else:
            t = state.write_table(prop["table"], source=f"e1 iteration {i}")
            v = t["version"]
            it["version"], it["share_changes"] = v, share_changes(prop["table"], inc_table, unit_ids)
            if prop["rules"]:
                state.save_rules(state.rules() + [{"text": r, "status": "trial", "source": f"e1 iteration {i}"} for r in prop["rules"]])
            inc_runs = [run_stats(rdir(a.task, r), diagnose(rdir(a.task, r))) for r in st["incumbent"]["rounds"]]
            names, runs, n = [], [], 0

            def trial(want: int) -> None:
                """``want`` usable rounds more; a void one is run again once."""
                nonlocal n
                batch = [f"e1-i{i}-v{v}-r{n + j + 1}" for j in range(want)]
                n += want
                run_rounds(a.task, a.set, a.tokens, v, batch)
                for b in batch:
                    if not usable(a.task, b):
                        it["rounds"].append({"round": b, "usable": False})
                        b = f"e1-i{i}-v{v}-r{n + 1}"
                        n += 1
                        run_rounds(a.task, a.set, a.tokens, v, [b])
                    if usable(a.task, b):
                        s = run_stats(rdir(a.task, b), diagnose(rdir(a.task, b)))
                        ok, why = better(s, inc_runs, eps_noise=st["eps_noise"])
                        runs.append(s)
                        names.append(b)
                        it["rounds"].append({"round": b, "usable": True, **s, "better": ok, "why": why})
                    else:
                        it["rounds"].append({"round": b, "usable": False})

            trial(2)
            d = decide(runs, inc_runs, eps_noise=st["eps_noise"]) if len(runs) == 2 else "more"
            if d == "more":                                  # mixed (or a round lost): one more
                trial(3 - len(runs) if len(runs) < 3 else 0)
                d = decide(runs, inc_runs, eps_noise=st["eps_noise"]) if len(runs) >= 2 else "reject"
                d = "reject" if d == "more" else d
            it["decision"] = d
            it["candidate_mean"], it["incumbent_mean"] = (mean_stats(runs) if runs else None), mean_stats(inc_runs)
            st["rounds"] += names
            if it["decision"] == "accept":
                state.set_current(v)
                st["incumbent"] = {"name": f"table v{v}", "version": v, "rounds": names}
        (out / f"iter_{i:02d}.json").write_text(json.dumps(it, indent=1, default=str))
        st["iterations"].append({"iteration": i, "version": it.get("version"), "decision": it["decision"]})
        stf.write_text(json.dumps(st, indent=1))
        print(json.dumps({"iteration": i, "decision": it["decision"], "version": it.get("version"),
                          "rounds": [(r["round"], r.get("frozen_pass_rate"), r.get("better")) for r in it["rounds"]]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
