#!/usr/bin/env python3
"""Freeze a task's suites from AdaMAS runs instead of authoring them (adamas_unit.import_suites). No model call.
    uv run python scripts/import_suites.py --task nl2_deepdiff --set adamas1 \\
        --run /root/projects/AdaMAS/outputs/hard_compare/full1-20261022T235608Z-nl2_deepdiff/nl2_deepdiff \\
        --run /root/projects/AdaMAS/outputs/hard_compare/full2-20261023T030138Z-nl2_deepdiff/nl2_deepdiff
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.import_suites import import_set  # noqa: E402
from adamas_unit.task import load_task  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--set", required=True)
    ap.add_argument("--run", action="append", required=True, help="an AdaMAS run's task directory (repeatable)")
    a = ap.parse_args()
    require_sandbox("suite import (runs the suites on the docs-only repository)")
    task = load_task(a.task)
    m = import_set(task, [Path(r) for r in a.run], ROOT / "outputs" / "suites" / a.task / a.set)
    print(json.dumps(m, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
