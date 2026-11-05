#!/usr/bin/env python3
"""The "沙盒越界警告" section of a task report (docs/sandbox_breach_log.md).

Reads each run's ``summary.json`` ``sandbox_breach`` block when the run wrote one, and scans the run
otherwise (without writing anything). Prints the section; ``--json`` prints the per-run summaries.

    uv run python scripts/sandbox_breach_report.py <run_dir> [<run_dir> ...]
    uv run python scripts/sandbox_breach_report.py --glob 'outputs/author_probe/author-A5-*/*'
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.sandbox.breach import LOG_NAME, report_section, scan_run, summary_of  # noqa: E402


def summary_for(run_dir: Path) -> dict:
    log = run_dir / LOG_NAME
    sj = run_dir / "summary.json"
    if log.is_file() and sj.is_file():
        sb = (json.loads(sj.read_text(encoding="utf-8")).get("sandbox_breach") or {})
        events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        s = summary_of(events, runs=1, commands=int(sb.get("commands") or 0))
        s.update(run_id=sb.get("run_id") or str(run_dir), void=s["critical"] > 0, manual_review=s["warning"] >= 5)
        return s
    return scan_run(run_dir, write=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*", type=Path)
    ap.add_argument("--glob", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    runs = list(a.runs) + ([Path(p) for p in sorted(glob.glob(a.glob))] if a.glob else [])
    runs = [r for r in runs if (r / "sandbox_manifest.json").is_file()]
    sums = [summary_for(r) for r in runs]
    print(json.dumps(sums, indent=1, ensure_ascii=False) if a.json else report_section(sums))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
