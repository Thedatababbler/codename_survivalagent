#!/usr/bin/env python3
"""Freeze a task's import-check manifest (patch 2026-11-09), once, before any round; all arms share it. Reads only
the design documents under the dataset repository's docs/. No model call. Prints the size and the hash.

    uv run python scripts/build_importcheck_manifest.py nl2_deepdiff
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.importcheck import build_manifest  # noqa: E402
from adamas_unit.task import load_task  # noqa: E402


def main() -> int:
    for t in sys.argv[1:]:
        m = build_manifest(load_task(t))
        print(json.dumps({"task": t, "sha256": m["sha256"], "names": len(m["names"]),
                          "with_module": sum(1 for e in m["names"] if e["module"]),
                          "without_module": sum(1 for e in m["names"] if not e["module"]),
                          "by_unit": dict(Counter(str(e["unit"]) for e in m["names"])),
                          "modules": len(m["modules"]), "cli_entries": len(m["cli"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
