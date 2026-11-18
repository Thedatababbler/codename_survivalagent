"""The round's memory file and the handoff order (refactor §2.3).

The executor keeps one file, ``AGENT_MEMORY.md`` in the repository root (ignored by git, removed from the final
code), with a fixed structure. The program checks the structure and, before a handoff or a compaction that relies
on it, that the file was written after the last change of the code -- the executor cannot skip that step.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

MEMORY_FILE = "AGENT_MEMORY.md"
HEADINGS = ("Done", "Decisions", "Files changed", "Unfinished", "Known problems")
WORKER_HEADING = "Worker results"

TEMPLATE = "# Memory\n\n" + "\n".join(f"## {h}\n\n(nothing yet)\n" for h in HEADINGS) + f"\n## {WORKER_HEADING}\n\n(none)\n"

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def memory_path(ws: Path) -> Path:
    return Path(ws) / MEMORY_FILE


def init_memory(ws: Path) -> None:
    p = memory_path(ws)
    if not p.exists():
        p.write_text(TEMPLATE, encoding="utf-8")


def read_memory(ws: Path) -> str:
    p = memory_path(ws)
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def structure_problems(text: str) -> list[str]:
    return [f"heading '## {h}' is missing" for h in HEADINGS if f"## {h}" not in text]


def code_hash(ws: Path) -> str:
    """A hash of the repository's files (paths, sizes, contents) without the memory file and caches: changes when
    the code changes."""
    h = hashlib.sha256()
    ws = Path(ws)
    for p in sorted(ws.rglob("*")):
        rel = p.relative_to(ws)
        if any(part in _SKIP_DIRS for part in rel.parts) or rel.name == MEMORY_FILE or p.suffix == ".pyc":
            continue
        if p.is_file() and not p.is_symlink():
            h.update(str(rel).encode())
            try:
                h.update(hashlib.sha256(p.read_bytes()).digest())
            except OSError:
                pass
    return h.hexdigest()


def memory_hash(ws: Path) -> str:
    return hashlib.sha256(read_memory(ws).encode()).hexdigest()


class MemoryTracker:
    """Whether the memory file is fresh: written (its content changed) after the code last changed."""

    def __init__(self, ws: Path):
        self.ws = Path(ws)
        self.code = code_hash(self.ws)
        self.mem = memory_hash(self.ws)
        self.fresh = False               # the template is not a memory
        self.steps_since_code_change = 0

    def observe(self) -> tuple[bool, bool]:
        """Call after every executed command: (code changed, memory changed)."""
        c, m = code_hash(self.ws), memory_hash(self.ws)
        code_changed, mem_changed = c != self.code, m != self.mem
        self.code, self.mem = c, m
        if code_changed:
            self.fresh = False
            self.steps_since_code_change = 0
        else:
            self.steps_since_code_change += 1
        if mem_changed:                  # written in the same command as code: counts as written after it
            self.fresh = not structure_problems(read_memory(self.ws))
        return code_changed, mem_changed

    def handoff_problems(self) -> list[str]:
        text = read_memory(self.ws)
        out = structure_problems(text)
        if not self.fresh:
            out.append("the memory file was not updated after the last change of the code")
        return out

    def append_worker_result(self, text: str) -> None:
        """A worker that did not write its result back: the program records what it returned."""
        cur = read_memory(self.ws) or TEMPLATE
        if f"## {WORKER_HEADING}" not in cur:
            cur += f"\n## {WORKER_HEADING}\n"
        memory_path(self.ws).write_text(cur.rstrip() + "\n\n" + text.strip() + "\n", encoding="utf-8")
        self.mem = memory_hash(self.ws)
