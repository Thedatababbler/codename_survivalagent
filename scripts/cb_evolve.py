#!/usr/bin/env python3
"""Step D: evolve memory-compaction and token-reminder on the ContextBench evolution set. SPENDS QUOTA.

    set -a; . ./.env; set +a
    uv run python scripts/cb_evolve.py --out outputs/cb_evo/e1 --instance kv_store:R4000:1001 [--instance ...] \
        --eps eps.json --baseline-round calib-1 --tokens 5000000 --max-tokens 120000000 --parallel 4

``--eps``: {family: eps} from the calibration. ``--baseline-round``: a round under outputs/cb whose pi_0 runs on the
evolution instances are taken as the baseline's (missing ones are run). Each combination's runs are one
``scripts/cb_run.py --parallel`` batch (round ``<out name>-<combination>``). Stops on a void run, on a run not graded
from snapshots, after 5 proposals in a row that leave the frontier unchanged, or when the runs of this evolution
used more than ``--max-tokens``. One line per generation. Rerun the same command to resume.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.model import complete, load_env  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402
from survival import cb as CB  # noqa: E402
from survival import cb_run as RUN  # noqa: E402
from survival.cb_evolve import CBEvolution  # noqa: E402

PI0 = ROOT / "configs" / "cb_skills_pi0"


def round_results(round_id: str, since: int = 0) -> list[dict]:
    """The result.json of every run of a round listed in its runs.jsonl from line ``since`` on, with ``void``."""
    f = RUN.OUT / round_id / "runs.jsonl"
    out = []
    for line in (f.read_text().splitlines()[since:] if f.is_file() else []):
        x = json.loads(line)
        r = json.loads((RUN.OUT / round_id / x["run"] / "result.json").read_text())
        r["void"] = bool(x.get("void"))
        out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--instance", action="append", required=True, help="family:level:seed (repeatable): the evolution set")
    ap.add_argument("--eps", required=True)
    ap.add_argument("--baseline-round", action="append", default=[])
    ap.add_argument("--tokens", type=int, required=True, help="cap of one run")
    ap.add_argument("--max-tokens", type=int, required=True, help="stop when the runs of this evolution used more")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--max-generations", type=int, default=30)
    a = ap.parse_args()
    load_env()
    require_sandbox("ContextBench evolution")
    CB.ensure()
    out = Path(a.out)
    instances = [(f, lv, int(s)) for f, lv, s in (x.split(":") for x in a.instance)]
    for f, lv, s in instances:
        CB.instance(f, lv, s)
    eps = json.loads(Path(a.eps).read_text())
    scans: list[str] = []

    def run_batch(cid: str, sdir: Path, todo: list[tuple[str, str, int]]) -> list[dict]:
        rid = f"{out.name}-{cid}"
        f = RUN.OUT / rid / "runs.jsonl"
        since = len(f.read_text().splitlines()) if f.is_file() else 0
        left = max(a.tokens, a.max_tokens - evo.state["run_tokens"])
        cmd = [sys.executable, str(ROOT / "scripts" / "cb_run.py"), "--round", rid, "--arm", "pi0" if cid == "pi0" else "evolved",
               "--skills", str(sdir), "--skills-id", cid, "--tokens", str(a.tokens), "--batch-tokens", str(left),
               "--parallel", str(max(2, a.parallel)), "--min-cap", str(a.tokens)]
        for fam, lv, s in todo:
            cmd += ["--run", f"{fam}:{lv}:{s}"]
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        (out / "batches").mkdir(parents=True, exist_ok=True)
        (out / "batches" / f"{rid}.log").write_text(p.stdout + "\n" + p.stderr, encoding="utf-8")
        scans.append(rid)
        return round_results(rid, since)

    baseline = [r for rid in a.baseline_round for r in round_results(rid)]
    evo = CBEvolution(out, instances=instances, run_batch=run_batch, complete=complete, eps=eps, base_skills=PI0,
                      baseline_runs=baseline)
    res = evo.run(a.max_generations, should_stop=lambda: "token cap of this evolution reached" if evo.state["run_tokens"] > a.max_tokens else "")
    print(json.dumps(res, indent=1))
    dirs = [d for rid in scans for d in sorted((RUN.OUT / rid).glob("r*")) if (d / "result.json").is_file()]
    print(breach.report_section([breach.scan_run(d, run_id=f"cb-{d.parent.name}-{d.name}", write=False) for d in dirs]))
    return 1 if res["stopped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
