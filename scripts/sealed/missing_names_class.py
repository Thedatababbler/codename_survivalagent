#!/usr/bin/env python3
"""Import-check patch §0 (2026-11-09, user request): the names whose absence kills a whole held-out file, in three
classes against the design documents. SEALED; counts only.

Feedback half only (the evaluation half is never touched). For each repo, each feedback-half file that collects none
of its half's functions with an attribute / name error (``heldout_collect_diag.category``): the first missing name
the error names, and the module it was imported from when the error says. Then, against the documents-only
inventory (``public_symbols.derive_public_symbols`` on the task's design documents):

  1. the documents name it and attribute it to a module;
  2. the documents name it without a module;
  3. the documents do not name it.

Extra counts (numbers only): of class 1, how many were imported from the module the documents state (or from the
package top level); of class 2, how many were imported from the package top level (the only case the patch's
check covers for class 2); of class 3, how many appear as a plain word somewhere in the documents; errors the
parser could not read. No name, file or case leaves.

    uv run python scripts/sealed/missing_names_class.py nl2_deepdiff <repo> [<repo> ...]
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
from heldout_collect_diag import category  # noqa: E402

ROOT = H.ROOT

_PATTERNS = [
    (re.compile(r"cannot import name '([\w.]+)' from '([\w.]+)'"), "import"),
    (re.compile(r"module '([\w.]+)' has no attribute '(\w+)'"), "module_attr"),
    (re.compile(r"type object '(\w+)' has no attribute '(\w+)'"), "class_attr"),
    (re.compile(r"'(\w+)' object has no attribute '(\w+)'"), "object_attr"),
    (re.compile(r"name '(\w+)' is not defined"), "name"),
]


def first_missing(text: str) -> tuple[str, str, str] | None:
    """(name, module or owner, kind) of the first error line that names a missing name."""
    best = None
    for pat, kind in _PATTERNS:
        m = pat.search(text)
        if m and (best is None or m.start() < best[0]):
            if kind == "import":
                best = (m.start(), m.group(1), m.group(2), kind)
            elif kind == "name":
                best = (m.start(), m.group(1), "", kind)
            else:
                best = (m.start(), m.group(2), m.group(1), kind)
    return None if best is None else best[1:]


def dead_texts(task: str, repo_src: Path) -> list[str]:
    """Collection output of every feedback-half file that collects none of its half's functions (attr/name kind)."""
    from orchestra.sandbox.guard import run_confined

    keys = H.load_split(task)["halves"]["feedback"]
    sub = G.heldout_subdir_name(task)
    py = G.task_python(task)
    by_file: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        by_file[k.split("::", 1)[0]].append(k)
    out = []
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
            try:
                p = run_confined(base + [f"{sub}/{f}", "--collect-only", "-q"], cwd=repo, mode="scoring",
                                 role="heldout_diag", capture_output=True, text=True, timeout=300, check=False, env=env)
                text = (p.stdout or "") + "\n" + (p.stderr or "")
            except subprocess.TimeoutExpired:
                continue
            got = {H._key_of(line.strip(), sub) for line in text.splitlines() if "::" in line} & set(ks)
            if not got and category(text, False) == "attribute_or_name_error":
                out.append(text)
    return out


def inventories(task: str) -> tuple[str, str, dict]:
    """Two documents-only readings of 'the documents name it (with a module)': AdaMAS ``public_symbols`` and the
    import-check manifest (``importcheck.build_manifest_from_docs``)."""
    from adamas_unit.importcheck import build_manifest_from_docs, manifest_inputs
    from adamas_unit.task import load_task
    from orchestra.codeprojecteval.public_symbols import derive_public_symbols, load_docs

    t = load_task(task)
    docs = load_docs(t.repo_root / "docs")        # the design documents the agents get; nothing sealed
    out = {}
    inv = derive_public_symbols(docs)
    a: dict[str, set[str]] = defaultdict(set)
    u: set[str] = set()
    for s in inv.symbols:
        for n in {s.name, s.name.split(".")[-1]}:
            if s.module:
                a[n].add(s.module.removesuffix(".py").replace("/", ".").removesuffix(".__init__"))
            else:
                u.add(n)
    out["public_symbols"] = (a, u)
    m = build_manifest_from_docs({p.name: p.read_text(encoding="utf-8", errors="replace") for p in manifest_inputs(t)},
                                 t.package, [(x.unit_id, list(x.focus_paths)) for x in t.units])
    a2: dict[str, set[str]] = defaultdict(set)
    u2: set[str] = set()
    for e in m["names"]:
        (a2[e["name"]].add(e["module"]) if e["module"] else u2.add(e["name"]))
    out["manifest"] = (a2, u2)
    return t.package, "\n".join(docs.values()), out


def classify(task: str, repos: list[str]) -> dict:
    pkg, alltext, invs = inventories(task)
    texts = [x for r in repos for x in dead_texts(task, Path(r))]
    res = {"task": task, "repos": len(repos), "dead_files_attr_or_name": len(texts)}
    for label, (attributed, unattributed) in invs.items():
        res[label] = count(texts, pkg, alltext, attributed, unattributed)
    return res


def count(texts: list[str], pkg: str, alltext: str, attributed: dict, unattributed: set) -> dict:
    c: Counter = Counter()
    uniq: dict[str, set] = defaultdict(set)
    if True:
        for text in texts:
            fm = first_missing(text)
            if fm is None:
                c["unparsed"] += 1
                continue
            name, where, kind = fm
            name = name.split(".")[-1]
            top = kind in ("import", "module_attr") and where == pkg
            if name in attributed:
                c["class1_named_with_module"] += 1
                uniq["class1"].add(name)
                if kind in ("import", "module_attr") and where in attributed[name]:
                    c["class1_imported_from_the_documented_module"] += 1
            elif name in unattributed:
                c["class2_named_without_module"] += 1
                uniq["class2"].add(name)
                if top:
                    c["class2_imported_from_top_level"] += 1
            else:
                c["class3_not_named"] += 1
                uniq["class3"].add(name)
                if re.search(rf"\b{re.escape(name)}\b", alltext):
                    c["class3_but_plain_word_in_docs"] += 1
            c[f"kind_{kind}"] += 1
    return {**dict(sorted(c.items())), **{f"distinct_{k}": len(v) for k, v in sorted(uniq.items())}}


def main() -> int:
    out = classify(sys.argv[1], sys.argv[2:])
    text = json.dumps(out)
    G.assert_no_source_overlap(text, sys.argv[1], allow_case_ids=False)
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
