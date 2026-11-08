#!/usr/bin/env python3
"""The held-out suite of one task split into a feedback half and an evaluation half (user decision 2026-11-08). SEALED.

Split: by test function (``<file>::<function>``, methods by name, parameters folded), stratified by the test module
(file): within each module the functions are ordered by a seeded hash and dealt alternately to the two halves
(a module's odd function goes to the half that is short). The split record stays under ``SEALED``; it is made once
and never rewritten.

Commands (numbers only on stdout):
    split    <task>                     make the split once; prints the two halves' sizes
    score    <task> <repo> [<repo>...]  feedback-half pass ratio of each repo (one number each)
    refcheck <task>                     positive control: the reference implementation's feedback-half ratio
    selfcheck <task>                    the split is reproducible (a second split from the same seed equals the
                                        stored one) and the halves do not overlap; prints booleans

The evaluation half is never scored here (E2 only). Nothing per case leaves this program: no ids, no counts per
module, no hit or miss -- one ratio per repo.

    uv run python scripts/sealed/heldout_half.py score nl2_deepdiff outputs/rounds/<round>/nl2_deepdiff/final
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard as G  # noqa: E402

SEALED = Path("/root/.adamas_unit_sealed")
SEED = 20261108
_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")


def _split_path(task: str) -> Path:
    return SEALED / f"split_{task}.json"


def case_keys(task: str) -> list[str]:
    """Every held-out test function as ``<file relative to the held-out dir>::<function>``."""
    d = G.heldout_dir(task)
    keys = set()
    for p in sorted({*d.rglob("test_*.py"), *d.rglob("*_test.py")}):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        rel = str(p.relative_to(d))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                keys.add(f"{rel}::{fn.name}")
    return sorted(keys)


def make_split(task: str, seed: int = SEED) -> dict:
    by_mod: dict[str, list[str]] = defaultdict(list)
    for k in case_keys(task):
        by_mod[k.split("::", 1)[0]].append(k)
    halves: dict[str, list[str]] = {"feedback": [], "evaluation": []}
    for m in sorted(by_mod):
        ks = sorted(by_mod[m], key=lambda k: hashlib.sha256(f"{seed}:{task}:{k}".encode()).hexdigest())
        first = "feedback" if len(halves["feedback"]) <= len(halves["evaluation"]) else "evaluation"
        second = "evaluation" if first == "feedback" else "feedback"
        for i, k in enumerate(ks):
            halves[first if i % 2 == 0 else second].append(k)
    return {"task": task, "seed": seed, "unit": "function", "strata": "test module (file)",
            "halves": {h: sorted(v) for h, v in halves.items()}}


def load_split(task: str) -> dict:
    p = _split_path(task)
    if p.is_file():
        return json.loads(p.read_text())
    s = make_split(task)
    SEALED.mkdir(parents=True, exist_ok=True)
    os.chmod(SEALED, 0o700)
    p.write_text(json.dumps(s, indent=1))
    return s


def _key_of(node: str, sub: str) -> str | None:
    parts = node.split("::")
    if len(parts) < 2:
        return None
    f = parts[0].split(sub + "/", 1)[-1] if (sub + "/") in parts[0] else parts[0]
    return f"{f}::{parts[-1].split('[', 1)[0]}"


def run_functions(task: str, repo_src: Path, keys: list[str], file_timeout: int = 600) -> tuple[int, int]:
    """(passed, total) among ``keys`` on a scratch copy of ``repo_src`` (confined, scoring mode). A function passes
    when every one of its nodes passed (skips allowed, at least one pass); one that did not run fails."""
    from orchestra.sandbox.guard import run_confined

    sub = G.heldout_subdir_name(task)
    py = G.task_python(task)
    want = set(keys)
    status: dict[str, list[str]] = defaultdict(list)
    scratch = ROOT / "outputs" / "sandbox"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(scratch)) as tmp:
        repo = Path(tmp) / task
        shutil.copytree(repo_src, repo, symlinks=True, ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", ".git", sub, "_unit_suite__*", "check_tests"))
        shutil.copytree(G.heldout_dir(task), repo / sub)
        (repo / "_heldout_pytest.ini").write_text("[pytest]\n", encoding="utf-8")       # pytest >= 8: no project config
        env = {"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
               "PATH": f"{py.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
        for f in sorted({k.split("::", 1)[0] for k in keys}):
            try:
                p = run_confined([str(py), "-m", "pytest", "-c", "_heldout_pytest.ini", "--rootdir", str(repo), f"{sub}/{f}",
                                  "-q", "-rA", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
                                  "--continue-on-collection-errors", "--timeout=30", "--timeout-method=signal"],
                                 cwd=repo, mode="scoring", role="heldout_half", capture_output=True, text=True,
                                 timeout=file_timeout, check=False, env=env)
                stdout = p.stdout
            except subprocess.TimeoutExpired:
                stdout = ""
            for line in stdout.splitlines():
                m = _LINE.match(line)
                if m and "::" in m.group(2):
                    k = _key_of(m.group(2), sub)
                    if k in want:
                        status[k].append(m.group(1))
    passed = sum(1 for k in keys if status.get(k) and all(x in ("PASSED", "XPASS", "SKIPPED") for x in status[k])
                 and any(x in ("PASSED", "XPASS") for x in status[k]))
    return passed, len(keys)


def feedback_ratio(task: str, repo: Path) -> float | None:
    ks = load_split(task)["halves"]["feedback"]
    p, t = run_functions(task, Path(repo), ks)
    return round(p / t, 4) if t else None


def _emit(task: str, out: dict) -> None:
    text = json.dumps(out)
    G.assert_no_source_overlap(text, task, allow_case_ids=False)
    print(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split")
    s.add_argument("task")
    c = sub.add_parser("score")
    c.add_argument("task")
    c.add_argument("repos", nargs="+")
    r = sub.add_parser("refcheck")
    r.add_argument("task")
    k = sub.add_parser("selfcheck")
    k.add_argument("task")
    a = ap.parse_args()
    if a.cmd == "split":
        s = load_split(a.task)
        _emit(a.task, {"task": a.task, "seed": s["seed"], "feedback": len(s["halves"]["feedback"]),
                       "evaluation": len(s["halves"]["evaluation"])})
    elif a.cmd == "score":
        _emit(a.task, {"task": a.task, "half": "feedback", "ratios": [feedback_ratio(a.task, Path(r)) for r in a.repos]})
    elif a.cmd == "refcheck":
        _emit(a.task, {"task": a.task, "half": "feedback", "reference_ratio": feedback_ratio(a.task, G.reference_root(a.task))})
    else:
        stored = load_split(a.task)
        again = make_split(a.task, stored["seed"])
        f, e = set(stored["halves"]["feedback"]), set(stored["halves"]["evaluation"])
        _emit(a.task, {"task": a.task, "reproducible": again["halves"] == stored["halves"], "disjoint": not (f & e),
                       "covers_all": (f | e) == set(case_keys(a.task)), "both_nonempty": bool(f) and bool(e)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
