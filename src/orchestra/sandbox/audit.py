# Copied from AdaMAS src/orchestra/sandbox/audit.py (34276d69) for AdaMAS-unit, 2026-11-05.
"""After-the-fact audit of agent commands (sandbox spec A, second line of defence).

Reads the commands an agent ran (captured turn items, and the Codex session logs
in each private CODEX_HOME) and lists those that:
- reach the external network;
- install or download packages;
- name a forbidden path;
- tamper with ``LD_PRELOAD`` (unset, override or export; the network guard's
  only bypass, user decision 2026-10-10).

``audit_run`` is called at the end of every sandboxed run and its result goes
into ``sandbox_manifest.json``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXTERNAL = re.compile(r"\b(curl|wget|git\s+clone|git\s+fetch|git\s+pull|pip3?\s+download|http\.client)\b[^\n]*https?://(?!127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])"
                      r"|urlopen\(\s*['\"]https?://(?!127\.0\.0\.1|localhost)")
INSTALL = re.compile(r"\b(pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+(install|download)\b|\bpoetry\s+add\b|\bconda\s+install\b|\bnpm\s+install\b"
                     r"|\bapt(-get)?\s+install\b")
LD_TAMPER = re.compile(r"(\benv\s+(-[a-zA-Z]*\s+)*-u\s*LD_PRELOAD\b|\bunset\s+LD_PRELOAD\b|\bLD_PRELOAD\s*=|\bexport\s+LD_PRELOAD\b"
                       r"|environ\[\s*['\"]LD_PRELOAD['\"]\s*\]\s*=|environ\.pop\(\s*['\"]LD_PRELOAD|del\s+os\.environ\[\s*['\"]LD_PRELOAD)")
FORBIDDEN = {
    "memory": str(ROOT / "memory") + "/", "configs": str(ROOT / "configs") + "/", "repo_source": str(ROOT / "src") + "/",
    "dataset_cpe": "/root/codex-benchmarks/projectgen/", "dataset_nl2": "/root/codex-benchmarks/nl2repo/cpe_format",
    "codex_home": "/root/.codex", "verification_suites": str(ROOT / "outputs" / "author_probe") + "/",
}


def classify(command: str, *, own_batch: str = "") -> list[str]:
    kinds = []
    if EXTERNAL.search(command):
        kinds.append("external_network")
    if INSTALL.search(command):
        kinds.append("package_install")
    if LD_TAMPER.search(command):
        kinds.append("ld_preload_tamper")
    bad = [k for k, p in FORBIDDEN.items() if p in command]
    run_id = Path(own_batch).name if own_batch else ""
    # the run's own paths (also after its directory was archived under another parent)
    outs = [m for m in re.findall(re.escape(str(ROOT / "outputs")) + r"/[^\s'\":]+", command)
            if not (own_batch and (m.startswith(own_batch) or (run_id and f"/{run_id}/" in m + "/")))]
    if outs:
        bad.append("other_run_outputs")
    if bad:
        kinds.append("forbidden_path:" + ",".join(sorted(set(bad))))
    return kinds


def commands_from_items(items: list) -> list[str]:
    out = []
    for it in items:
        if isinstance(it, dict):
            for k in ("command", "cmd"):
                v = it.get(k)
                if isinstance(v, list):
                    v = " ".join(map(str, v))
                if isinstance(v, str):
                    out.append(v)
            for v in it.values():
                if isinstance(v, (list, dict)):
                    out += commands_from_items(v if isinstance(v, list) else [v])
    return out


def commands_from_session(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        pl = e.get("payload") or {}
        if pl.get("type") == "function_call":
            try:
                args = json.loads(pl.get("arguments") or "{}")
            except ValueError:
                args = {"cmd": pl.get("arguments")}
            c = args.get("cmd") or args.get("command") or args.get("input") or ""
            out.append(" ".join(map(str, c)) if isinstance(c, list) else str(c))
    return out


def run_commands(run_dir: Path) -> Iterable[tuple[str, str]]:
    """(source file, command) for every captured command of a run."""
    for f in sorted(Path(run_dir).glob("tasks/*/backend_traces/*/*.items.json")):
        try:
            for c in commands_from_items(json.loads(f.read_text(errors="replace"))):
                yield str(f), c
        except ValueError:
            continue
    for f in sorted(Path(run_dir).glob("tasks/*/backend_traces/*/.sandbox/*/codex_home/sessions/**/*.jsonl")):
        for c in commands_from_session(f.read_text(errors="replace")):
            yield str(f), c


def audit_run(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    batch = str(run_dir.parent)
    findings, n = [], 0
    seen = set()
    for src, c in run_commands(run_dir):
        if (src.split("/.sandbox/")[0], c) in seen:
            continue
        seen.add((src.split("/.sandbox/")[0], c))
        n += 1
        kinds = classify(c, own_batch=batch)
        if kinds:
            findings.append({"file": src, "kinds": kinds, "command": c[:300]})
    return {"commands": n, "findings": findings, "ok": not findings}


__all__ = ["audit_run", "classify", "commands_from_items", "commands_from_session", "run_commands"]
