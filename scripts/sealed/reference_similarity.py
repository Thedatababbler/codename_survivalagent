#!/usr/bin/env python3
"""Similarity of a round's final code to the task's reference implementation. SEALED.

Tokenisation, 5-gram sets, reference-weighted Jaccard and the outlier cut are AdaMAS's
(``scripts/sealed/reference_similarity.py`` at 34276d69): comments, docstrings and whitespace dropped, names kept;
outlier above min(Q3 + 3 * IQR, 1.8 * median, 0.6) over the non-empty finals of the task's valid rounds.

    check <task> <repo> --population <repo> [<repo> ...]
        {"similarity": x, "cut": c, "outlier": bool, "population": n} -- numbers only; used by the breach scanner's
        void rule (CLAUDE.md rule 6, 2026-11-08: a refused, verified-empty search keeps the run only when this is
        not an outlier)
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import reference_root, task_config  # noqa: E402

N = 5
_SKIP = ("test", "tests", "unit_tests", "check_tests", "spec_tests", "docs", ".git", "__pycache__", "bin", "lib", "lib64",
         "include", "share", ".venv", "venv", "build", "dist", "examples")


def tokens(src: str) -> list[str]:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out
    prev = None
    for t in toks:
        if t.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
                      tokenize.ENDMARKER):
            continue
        if t.type == tokenize.STRING and (prev is None or prev in (":", "\n")):
            prev = "str"
            continue
        out.append("S" if t.type == tokenize.STRING else t.string)
        prev = t.string
    return out


def grams(root: Path, pkg: str) -> set[tuple]:
    g: set[tuple] = set()
    for p in Path(root).rglob("*.py"):
        rel = p.relative_to(root).parts
        if any(part in _SKIP or part.startswith(("test_", "_unit_suite__")) for part in rel) or p.name == "conftest.py":
            continue
        if not (rel[0] == pkg or rel[0] == f"{pkg}.py" or (rel[0] == "src" and len(rel) > 1 and rel[1] in (pkg, f"{pkg}.py"))):
            continue
        try:
            t = tokens(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        g |= {tuple(t[i:i + N]) for i in range(max(0, len(t) - N + 1))}
    return g


def cut_of(vals: list[float]) -> float:
    nz = sorted(v for v in vals if v > 0)
    if len(nz) >= 4:
        qs = statistics.quantiles(nz, n=4)
        q1, q3 = qs[0], qs[2]
    else:
        q1, q3 = (min(nz), max(nz)) if nz else (0.0, 0.0)
    med = statistics.median(nz) if nz else 0.0
    cut = min(q3 + 3 * (q3 - q1), 0.6) if len(nz) >= 4 else 0.6
    return min(cut, 1.8 * med) if med and len(nz) >= 3 else cut


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("task")
    c.add_argument("repo")
    c.add_argument("--population", nargs="+", required=True)
    a = ap.parse_args()
    pkg = Path(str(task_config(a.task).get("source_code") or a.task.removeprefix("nl2_"))).name.removesuffix(".py")
    ref = grams(reference_root(a.task), pkg)

    def sim(r: str) -> float:
        g = grams(Path(r), pkg)
        return round(len(g & ref) / len(ref), 4) if g and ref else 0.0
    pop = [sim(r) for r in a.population if Path(r).resolve() != Path(a.repo).resolve()]
    s, cut = sim(a.repo), cut_of(pop)
    print(json.dumps({"similarity": s, "cut": round(cut, 4), "outlier": s > cut, "population": len(pop)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
