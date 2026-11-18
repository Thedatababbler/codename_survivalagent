#!/usr/bin/env python3
"""Freeze the import manifests of the v0.3 tasks (documents only; no model, no quota).

    uv run python scripts/v03_build_manifests.py [task ...]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit import importcheck as IC  # noqa: E402
from survival.tasks import EVOLUTION_TASKS, MANIFESTS, load_task  # noqa: E402

for t in sys.argv[1:] or EVOLUTION_TASKS:
    m = IC.build_manifest(load_task(t), out_dir=MANIFESTS / t)
    print(json.dumps({"task": t, "sha256": m["sha256"], "names": len(m["names"]),
                      "with_module": sum(1 for e in m["names"] if e["module"]), "modules": len(m.get("modules") or [])}))
