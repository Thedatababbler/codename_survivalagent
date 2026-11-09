#!/usr/bin/env python3
"""Diagnosis (2026-11-09, user request): why the feedback-half ratio varies. SEALED; numbers and categories only.

For each repo, on the feedback half only (the evaluation half is never touched): per held-out file of the half,
``pytest --collect-only`` then a run (scoring mode, confined). Output per repo: functions in the half, functions
collected, functions passed, the ratio (passed / all, as ``heldout_half.score``), files of the half, files that
collect none of their half's functions, and those files' failure categories (import error / attribute or name
error / collection error / timeout / other) as counts. No file name, case name or test text leaves.

    uv run python scripts/sealed/heldout_collect_diag.py nl2_deepdiff <repo> [<repo> ...]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard as G  # noqa: E402
import heldout_half as H  # noqa: E402

ROOT = H.ROOT


def category(text: str, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if re.search(r"cannot import name|AttributeError|NameError", text):
        return "attribute_or_name_error"
    if re.search(r"ModuleNotFoundError|No module named|ImportError", text):
        return "import_error"
    if re.search(r"ERROR collecting|error(s)? during collection|Interrupted", text):
        return "collection_error"
    return "other"


def diag(task: str, repo_src: Path) -> dict:
    from orchestra.sandbox.guard import run_confined

    keys = H.load_split(task)["halves"]["feedback"]
    sub = G.heldout_subdir_name(task)
    py = G.task_python(task)
    by_file: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        by_file[k.split("::", 1)[0]].append(k)
    collected: set[str] = set()
    status: dict[str, list[str]] = defaultdict(list)
    cats: Counter = Counter()
    dead_files = 0
    scratch = ROOT / "outputs" / "sandbox"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(scratch)) as tmp:
        repo = Path(tmp) / task
        shutil.copytree(repo_src, repo, symlinks=True, ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", ".git", sub, "_unit_suite__*", "check_tests"))
        shutil.copytree(G.heldout_dir(task), repo / sub)
        (repo / "_heldout_pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        env = {"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
               "PATH": f"{py.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
        base = [str(py), "-m", "pytest", "-c", "_heldout_pytest.ini", "--rootdir", str(repo), "-p", "no:cacheprovider",
                "-o", "addopts="]
        for f, ks in sorted(by_file.items()):
            want = set(ks)
            timed_out = False
            try:
                p = run_confined(base + [f"{sub}/{f}", "--collect-only", "-q"], cwd=repo, mode="scoring",
                                 role="heldout_diag", capture_output=True, text=True, timeout=300, check=False, env=env)
                text = (p.stdout or "") + "\n" + (p.stderr or "")
            except subprocess.TimeoutExpired:
                text, timed_out = "", True
            got = {H._key_of(line.strip(), sub) for line in text.splitlines() if "::" in line} & want
            collected |= got
            if not got:
                dead_files += 1
                cats[category(text, timed_out)] += 1
                continue
            try:
                r = run_confined(base + [f"{sub}/{f}", "-q", "-rA", "--no-header", "--continue-on-collection-errors",
                                         "--timeout=30", "--timeout-method=signal"], cwd=repo, mode="scoring",
                                 role="heldout_diag", capture_output=True, text=True, timeout=600, check=False, env=env)
                out = r.stdout or ""
            except subprocess.TimeoutExpired:
                out = ""
            for line in out.splitlines():
                m = H._LINE.match(line)
                if m and "::" in m.group(2):
                    k = H._key_of(m.group(2), sub)
                    if k in want:
                        status[k].append(m.group(1))
    passed = sum(1 for k in keys if status.get(k) and all(x in ("PASSED", "XPASS", "SKIPPED") for x in status[k])
                 and any(x in ("PASSED", "XPASS") for x in status[k]))
    return {"functions": len(keys), "collected": len(collected), "passed": passed,
            "collect_rate": round(len(collected) / len(keys), 4), "pass_after_collect": round(passed / len(collected), 4) if collected else None,
            "ratio": round(passed / len(keys), 4), "files": len(by_file), "dead_files": dead_files,
            "dead_file_categories": dict(cats)}


def main() -> int:
    task, repos = sys.argv[1], sys.argv[2:]
    for r in repos:
        out = {"repo": r, **diag(task, Path(r))}
        text = json.dumps(out)
        G.assert_no_source_overlap(text, task, allow_case_ids=False)
        print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
