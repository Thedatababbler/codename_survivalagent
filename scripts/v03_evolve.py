#!/usr/bin/env python3
"""Evolve the planner's skill documents on the evolution tasks (refactor §6). SPENDS QUOTA.

    uv run python scripts/v03_evolve.py --out outputs/evo_v03/e1 --budgets budgets.json --eps eps.json \
        --max-generations 20 --max-tokens 60000000

``--budgets``: {task: B} from the calibration (§7); ``--eps``: {task: eps} from pi_0's baseline rounds. Stops on an
unusable (void) run, after 5 proposals in a row that leave the frontier unchanged, or when the rounds' tokens pass
``--max-tokens``. Rerun the same command to resume.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from adamas_unit.model import complete, load_env  # noqa: E402
from survival.evolve import Evolution  # noqa: E402
from survival.tasks import EVOLUTION_TASKS, forbidden_names  # noqa: E402
from v03_run_round import PI0, run_round  # noqa: E402


def leak_check(text: str) -> bool:
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "sealed" / "skill_leak_check.py"), *EVOLUTION_TASKS],
                       input=text, capture_output=True, text=True, cwd=ROOT, timeout=600)
    try:
        return bool(json.loads(p.stdout.strip().splitlines()[-1])["ok"])
    except Exception:  # noqa: BLE001 - a check that did not run is not a pass
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--budgets", required=True)
    ap.add_argument("--eps", required=True)
    ap.add_argument("--window", type=int, default=64_000)
    ap.add_argument("--max-generations", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, required=True, help="stop when the rounds of this evolution used more")
    a = ap.parse_args()
    load_env()
    out = Path(a.out)
    budgets = json.loads(Path(a.budgets).read_text())
    eps = json.loads(Path(a.eps).read_text())
    spent = {"tokens": 0}

    def runner(cid: str, sdir: Path, task: str, rep: int) -> dict:
        r = run_round(task_id=task, round_id=f"{out.name}-{cid}-r{rep}", arm="pi0" if cid == "pi0" else "evolved",
                      tokens=int(budgets[task]), window=a.window, skills_dir=sdir, skills_id=cid)
        spent["tokens"] += int(r["resources"]["tokens"])
        r.pop("breach_section", None)
        return r

    evo = Evolution(out, tasks=list(EVOLUTION_TASKS), runner=runner, complete=complete, eps=eps, base_skills=PI0,
                    forbidden=forbidden_names(), leak_check=leak_check)
    res = evo.run(a.max_generations, should_stop=lambda: "token cap of this evolution reached" if spent["tokens"] > a.max_tokens else "")
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
