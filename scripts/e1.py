#!/usr/bin/env python3
"""E1: the budget evolver on one task at one B (DESIGN §13; user decisions 2026-11-07, 2026-11-08). SPENDS QUOTA.

    uv run python scripts/e1.py --task nl2_deepdiff --set adamas2 --tokens 1586206 \
        --baseline tier50-pi0-r3,tier50-pi0-r4,tier50-pi0-r5 --out outputs/e1v2 \
        [--import-first outputs/e1/nl2_deepdiff/iter_01.json] [--iterations 5]

Per iteration: the evolver reads the records of the baseline and of this E1's candidate rounds (numbers only, units
as U1..Un, each with its held-out feedback-half ratio) and the earlier candidates' tables with their decisions, and
proposes a table (shares, repair limits, approval parameters). The table is stored as a new version (never current
while under trial) and run in 2 rounds side by side; ``evolver.decide`` (submittable, then the feedback-half ratio
against the incumbent's mean + eps) accepts, rejects or asks a 3rd round. Accepted: it becomes current and the
incumbent, and eps is recomputed from its rounds. At most ``--iterations`` iterations, and E1 stops after 2
rejections in a row. A void round is run again once. Recorded only, never deciding: frozen figures, gated units,
waste, "direction" (not shown to the evolver).

``--import-first``: iteration 1 is an iteration already run (its rounds exist); it is judged again by the rule here
(only the feedback ratios are computed, nothing is run) and marked as proposed with the old prompt.
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
from adamas_unit.evolver import better, decide, direction, eps_of, mean_stats, propose, round_record, run_stats  # noqa: E402
from adamas_unit.feedback import feedback_ratio  # noqa: E402
from adamas_unit.ledger import APPROVAL_DEFAULTS  # noqa: E402
from adamas_unit.model import complete  # noqa: E402
from adamas_unit.planner_state import PlannerState  # noqa: E402
from adamas_unit.task import load_task  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402

OLD_PROMPT = "proposed with the old evolver prompt (third priority in the scoring text, real unit names)"


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


def stats(task: str, rnd: str, last_unit: str) -> dict:
    d = rdir(task, rnd)
    return run_stats(d, diagnose(d), last_unit, hidden=feedback_ratio(task, rnd, d))


def share_changes(table: dict, incumbent: dict | None, unit_ids: list[str]) -> dict:
    inc = (incumbent or {}).get("units") or {u: {"tokens": 1 / len(unit_ids), "seconds": 1 / len(unit_ids),
                                                  "compressions": 1 / len(unit_ids), "repairs": 3} for u in unit_ids}
    return {u: {k: [round(float(inc[u].get(k, 0)), 4), round(float(table[u][k]), 4)] for k in ("tokens", "seconds", "compressions", "repairs")
                if float(inc[u].get(k, 0)) != float(table[u][k])} for u in unit_ids}


def approval_changes(approval: dict | None, incumbent: dict | None) -> dict:
    a = {**APPROVAL_DEFAULTS, **(approval or {})}
    b = {**APPROVAL_DEFAULTS, **((incumbent or {}).get("approval") or {})}
    return {k: [b[k], a[k]] for k in a if a[k] != b[k]}


def judge(it: dict, task: str, names: list[str], inc_runs: list[dict], eps: float, unit_ids: list[str]) -> str:
    """Decide on the usable rounds ``names`` of iteration ``it`` (filled in place)."""
    runs = []
    for r in it["rounds"]:
        if r.get("usable") and r["round"] in names:
            s = stats(task, r["round"], unit_ids[-1])
            r.update(s, better_d8=better(s, inc_runs, eps_noise=0)[0])
            runs.append(s)
    d, why = decide(runs, inc_runs, eps=eps)
    it["decision"], it["why"] = d, why
    it["direction"] = direction(runs, inc_runs)            # recorded only; not shown to the evolver
    it["candidate_mean"], it["incumbent_mean"] = (mean_stats(runs) if runs else None), mean_stats(inc_runs)
    it["candidate_hidden"] = [r["hidden"] for r in runs]
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--set", required=True)
    ap.add_argument("--tokens", type=int, required=True, help="the round budget B (tokens)")
    ap.add_argument("--baseline", required=True, help="comma-separated pi_0 rounds at this B")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "e1v2"))
    ap.add_argument("--import-first", default="", help="an earlier iteration file to take as iteration 1")
    ap.add_argument("--iterations", type=int, default=5)
    ap.add_argument("--judge-only", action="store_true", help="set up the state (and judge an imported iteration); run nothing")
    a = ap.parse_args()
    require_sandbox("E1")
    task = load_task(a.task)
    unit_ids = [u.unit_id for u in task.units]
    state = PlannerState(a.task)
    out = Path(a.out) / a.task
    out.mkdir(parents=True, exist_ok=True)
    stf = out / "e1_state.json"
    if stf.is_file():
        st = json.loads(stf.read_text())
    else:
        base = [r for r in a.baseline.split(",") if usable(a.task, r)]
        inc_hidden = [stats(a.task, r, unit_ids[-1])["hidden"] for r in base]
        # list(...) everywhere: the incumbent's rounds must never alias the growing history (2026-11-08 defect)
        st = {"task": a.task, "B_tokens": a.tokens, "rounds": list(base), "eps": eps_of(inc_hidden),
              "incumbent": {"name": "pi_0", "version": 0, "rounds": list(base), "hidden": inc_hidden}, "iterations": []}
        if a.import_first:
            it = json.loads(Path(a.import_first).read_text())
            it = {**it, "imported_from": a.import_first, "evolver_prompt": OLD_PROMPT,
                  "old_decision": it.get("decision"), "decision": None}
            inc_runs = [stats(a.task, r, unit_ids[-1]) for r in st["incumbent"]["rounds"]]
            names = [r["round"] for r in it["rounds"] if r.get("usable")]
            d = judge(it, a.task, names, inc_runs, st["eps"], unit_ids)
            it["eps"], it["incumbent"] = st["eps"], "pi_0"
            st["rounds"] += names
            if d == "accept":
                state.set_current(it["version"])
                hid = [stats(a.task, r, unit_ids[-1])["hidden"] for r in names]
                st["incumbent"] = {"name": f"table v{it['version']}", "version": it["version"], "rounds": list(names), "hidden": hid}
                st["eps"] = eps_of(hid)
            (out / "iter_01.json").write_text(json.dumps(it, indent=1, default=str))
            st["iterations"].append({"iteration": 1, "version": it.get("version"), "decision": d,
                                     "direction_ok": it["direction"]["ok"], "imported": True})
    stf.write_text(json.dumps(st, indent=1))
    if st["incumbent"]["version"]:
        state.set_current(st["incumbent"]["version"])
    else:
        state.clear_current()                                # the incumbent is pi_0: no current table
    if a.judge_only:
        print(json.dumps({"incumbent": st["incumbent"], "eps": st["eps"], "iterations": st["iterations"]}, indent=1))
        return 0

    def two_rejections() -> bool:
        d = [x["decision"] for x in st["iterations"]]
        return len(d) >= 2 and d[-1] != "accept" and d[-2] != "accept"

    while len(st["iterations"]) < a.iterations and not two_rejections():
        i = len(st["iterations"]) + 1
        records = [round_record(rdir(a.task, r), diagnose(rdir(a.task, r)), feedback_ratio(a.task, r, rdir(a.task, r)))
                   for r in st["rounds"] if usable(a.task, r)]
        inc_v = st["incumbent"]["version"]
        inc_table = state.table_version(inc_v) if inc_v else None
        trials = [{"version": x["version"], "table": state.table_version(x["version"]).get("units"),
                   "approval": state.table_version(x["version"]).get("approval"), "decision": x["decision"]}
                  for x in st["iterations"] if x.get("version")]
        prop = propose(complete, inc_table, records, unit_ids, trials=trials)
        it = {"iteration": i, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "incumbent": st["incumbent"]["name"],
              "eps": st["eps"], "evolver_prompt": "2026-11-08 (two priorities, units as U1..Un)",
              "proposal": {k: prop[k] for k in ("table", "approval", "reason", "rules", "problems", "tokens")}, "rounds": [],
              "decision": None}
        if prop["table"] is None:
            it["decision"] = "no valid proposal"
        else:
            v = state.write_table(prop["table"], source=f"e1v2 iteration {i}", approval=prop["approval"])["version"]
            it["version"], it["share_changes"] = v, share_changes(prop["table"], inc_table, unit_ids)
            it["approval_changes"] = approval_changes(prop["approval"], inc_table)
            if prop["rules"]:
                state.save_rules(state.rules() + [{"text": r, "status": "trial", "source": f"e1v2 iteration {i}"} for r in prop["rules"]])
            inc_runs = [stats(a.task, r, unit_ids[-1]) for r in st["incumbent"]["rounds"]]
            names: list[str] = []
            n = 0

            def trial(want: int) -> None:
                """``want`` usable rounds more; a void one is run again once."""
                nonlocal n
                batch = [f"e1v2-i{i}-v{v}-r{n + j + 1}" for j in range(want)]
                n += want
                run_rounds(a.task, a.set, a.tokens, v, batch)
                for b in batch:
                    if not usable(a.task, b):
                        it["rounds"].append({"round": b, "usable": False})
                        b = f"e1v2-i{i}-v{v}-r{n + 1}"
                        n += 1
                        run_rounds(a.task, a.set, a.tokens, v, [b])
                    it["rounds"].append({"round": b, "usable": usable(a.task, b)})
                    if usable(a.task, b):
                        names.append(b)

            trial(2)
            d = judge(it, a.task, names, inc_runs, st["eps"], unit_ids)
            if d == "more":
                trial(max(1, 3 - len(names)))
                d = judge(it, a.task, names, inc_runs, st["eps"], unit_ids)
                if d == "more":
                    it["decision"] = d = "reject"
            st["rounds"] += names
            if d == "accept":
                state.set_current(v)
                hid = [stats(a.task, r, unit_ids[-1])["hidden"] for r in names]
                st["incumbent"] = {"name": f"table v{v}", "version": v, "rounds": list(names), "hidden": hid}
                st["eps"] = eps_of(hid)
        (out / f"iter_{i:02d}.json").write_text(json.dumps(it, indent=1, default=str))
        st["iterations"].append({"iteration": i, "version": it.get("version"), "decision": it["decision"],
                                 "direction_ok": (it.get("direction") or {}).get("ok")})
        stf.write_text(json.dumps(st, indent=1))
        print(json.dumps({"iteration": i, "decision": it["decision"], "version": it.get("version"),
                          "hidden": it.get("candidate_hidden"), "why": it.get("why")}), flush=True)
    print(json.dumps({"stopped": "two rejections in a row" if two_rejections() else "iterations", "incumbent": st["incumbent"]["name"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
