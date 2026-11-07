"""Progress probes during a session (DESIGN §8.4, §5.3; USING §4 item 4).

Every ``probe_every`` model calls the agent hands itself to ``Prober.__call__``:

- the code changed since the last probe (files and lines, by comparing the workspace's text files with the copy the
  last probe kept; the driver only reads files, it runs nothing in the agent's directory -- in particular no git);
- the failures of the unit's own frozen suite, run by the gate on its own copy of the workspace (``run_gate`` copies
  first), in the sandbox; never in the agent's directory;
- the time spent is harness overhead (``Ledger.side_charge("harness")``): the agents' clock does not pay for it.

Nothing of it is shown to the agent. A stalled session is put to the planner (``decide_reclaim``); "reclaim" ends it.
"""

from __future__ import annotations

import difflib
import time
from pathlib import Path

from adamas_unit.gate import drop, run_gate
from adamas_unit.ledger import Ledger
from adamas_unit.planner import BudgetPlanner, Probe, SessionProgress

TEXT_SUFFIXES = {".py", ".txt", ".md", ".rst", ".cfg", ".toml", ".ini", ".json", ".yaml", ".yml"}
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache"}


def text_files(root: Path, limit: int = 2_000_000) -> dict[str, list[str]]:
    out = {}
    root = Path(root)
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if SKIP_PARTS & set(rel.parts) or not p.is_file() or p.is_symlink() or p.suffix not in TEXT_SUFFIXES:
            continue
        try:
            if p.stat().st_size <= limit:
                out[str(rel)] = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
    return out


def change(before: dict[str, list[str]], after: dict[str, list[str]]) -> tuple[int, int]:
    """(files changed, lines added + removed) between two text-file maps."""
    files = lines = 0
    for f in set(before) | set(after):
        a, b = before.get(f, []), after.get(f, [])
        if a == b:
            continue
        files += 1
        lines += sum(1 for d in difflib.ndiff(a, b) if d[:1] in "+-")
    return files, lines


class Prober:
    def __init__(self, *, ledger: Ledger, planner: BudgetPlanner | None, workspace: Path, suites: dict[str, Path],
                 venv: Path, work_dir: Path, gate_timeout: float, deselect: dict[str, list[str]] | None = None):
        """``suites``: the unit's own frozen suite ({unit_id: dir}; the single-session arm: all of them);
        ``deselect``: the cases no gate counts (vacuous, soft), left out of the probe as well."""
        self.ledger, self.planner = ledger, planner
        self.deselect = dict(deselect or {})
        self.workspace, self.suites = Path(workspace), dict(suites)
        self.venv, self.work_dir, self.gate_timeout = Path(venv), Path(work_dir), gate_timeout
        self.sessions: dict[str, SessionProgress] = {}
        self._last: dict[str, dict[str, list[str]]] = {}

    def start(self, lease_id: str, unit: int, role: str) -> SessionProgress:
        prog = SessionProgress(lease_id=lease_id, unit=unit, role=role)
        self.sessions[lease_id] = prog
        self._last[lease_id] = text_files(self.workspace)
        return prog

    def get(self, lease_id: str) -> SessionProgress | None:
        return self.sessions.get(lease_id)

    def __call__(self, agent) -> str:
        lid = agent.lease.lease_id
        prog = self.sessions.get(lid) or self.start(lid, agent.lease.unit, agent.lease.role)
        t0 = time.time()
        now = text_files(self.workspace)
        files, lines = change(self._last.get(lid, {}), now)
        self._last[lid] = now
        try:
            r = drop(run_gate(self.workspace, self.suites, venv=self.venv, work_dir=self.work_dir,
                              timeout=self.gate_timeout), [n for ids in self.deselect.values() for n in ids])
            failures = len(r.failed())
        except Exception:                       # a probe that cannot run is recorded as unknown, never as progress
            failures = None
        dt = time.time() - t0
        prog.probes.append(Probe(calls=agent.n_calls, failures=failures, files_changed=files, lines_changed=lines,
                                 seconds=round(dt, 2)))
        self.ledger.side_charge("harness", seconds=dt, what=f"progress probe {lid}", lease_id=lid)
        self.ledger.note("probe", lease=lid, calls=agent.n_calls, failures=failures, files_changed=files,
                         lines_changed=lines, seconds=round(dt, 2))
        if self.planner is not None and self.planner.decide_reclaim(prog):
            return "reclaim"
        return ""
