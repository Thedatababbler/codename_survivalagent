#!/usr/bin/env python3
"""Leak check of a skill document (refactor §4.5). SEALED; prints booleans and counts only.

The text on stdin may share no 20-character window with the held-out suite or the reference implementation of any
named task. Exempt (general expressions): windows that also occur in the task's design documents, which every agent
reads, and windows without a word of four letters.

    uv run python scripts/sealed/skill_leak_check.py <task> [<task> ...] < SKILL.md
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard as G  # noqa: E402


def source_windows(task: str) -> set[str]:
    out = set(G.heldout_source_windows(task))
    cfg = G.task_config(task)
    ref = G.reference_root(task) / str(cfg.get("source_code") or "")
    if cfg.get("source_code") and ref.exists():
        for p in ([ref] if ref.is_file() else ref.rglob("*.py")):
            out |= G._windows(p.read_text(encoding="utf-8", errors="replace"))
    return out


def public_windows(task: str) -> set[str]:
    out: set[str] = set()
    docs = G.dataset_root_for(task) / task / "docs"
    for p in docs.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".md", ".txt", ".rst"):
            out |= G._windows(p.read_text(encoding="utf-8", errors="replace"))
    return out


def main() -> int:
    text = sys.stdin.read()
    w = G._windows(text)
    res = {}
    for t in sys.argv[1:]:
        hit = (w & source_windows(t)) - public_windows(t)
        hit = {x for x in hit if re.search(r"[A-Za-z]{4,}", x)}
        res[t] = len(hit)
    print(json.dumps({"ok": not any(res.values()), "overlapping_windows": res}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
