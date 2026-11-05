# Copied from AdaMAS src/orchestra/sandbox/gate_tmp.py (34276d69) for AdaMAS-unit, 2026-11-05.
"""/tmp for the gate's tests of a task whose dataset tests hard-code it (user decision 2026-10-15).

bplustree's own ``check_tests/conftest.py`` writes ``/tmp/bplustree-testfile-check.index``. Under the sandbox the
gate may write only its workspace and a private tmp, so that stage could never pass. For the tasks listed under
``gate_tmp`` in configs/sandbox.yaml -- and only there -- the gate (the check script: the milestone gate and the
predecessor-suite re-runs) may write /tmp; agents never may. Held-out scoring already has unrestricted file access.
Either way the task's /tmp files are removed before and after each run, and runs of one task never overlap
(an exclusive lock per task).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import glob
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCK_DIR = ROOT / "outputs" / "sandbox"


def rules() -> dict[str, list[str]]:
    """task id -> globs of the /tmp files its dataset tests write."""
    from orchestra.sandbox.policy import load_config

    raw = (load_config().get("sandbox") or {}).get("gate_tmp") or {}
    return {str(k): [str(g) for g in (v or [])] for k, v in raw.items() if all(str(g).startswith("/tmp/") for g in (v or []))}


def task_of_command(command: list[str]) -> str | None:
    """The task a check-script command grades, from its manifest's package names; None for anything else."""
    if "--manifest" not in command:
        return None
    try:
        manifest = json.loads(Path(command[command.index("--manifest") + 1]).read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError):
        return None
    names = {str(manifest.get("source_dir") or "")} | {str(p) for p in manifest.get("top_level_packages") or []}
    return next((t for t in rules() if t in names), None)


def _clean(globs: list[str]) -> None:
    for pattern in globs:
        for p in glob.glob(pattern):
            with contextlib.suppress(OSError):
                os.unlink(p)


def _lock(task: str):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return open(LOCK_DIR / f"gate_tmp.{task}.lock", "w")  # noqa: SIM115 -- held for the session


@contextlib.contextmanager
def session(task: str | None) -> Iterator[list[str]]:
    """Exclusive, cleaned /tmp session for ``task``; yields the extra write grant (``["/tmp"]`` or ``[]``)."""
    globs = rules().get(task or "")
    if not globs:
        yield []
        return
    fh = _lock(str(task))
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _clean(globs)
        yield ["/tmp"]
    finally:
        _clean(globs)
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


@contextlib.asynccontextmanager
async def session_for_command(command: list[str]) -> AsyncIterator[list[str]]:
    """``session`` for a gate command, without blocking the event loop while another run holds the lock."""
    task = task_of_command(command)
    globs = rules().get(task or "")
    if not globs:
        yield []
        return
    fh = _lock(str(task))
    try:
        await asyncio.to_thread(fcntl.flock, fh, fcntl.LOCK_EX)
        _clean(globs)
        yield ["/tmp"]
    finally:
        _clean(globs)
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


__all__ = ["rules", "session", "session_for_command", "task_of_command"]
