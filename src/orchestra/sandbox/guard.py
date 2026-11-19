# Copied from AdaMAS src/orchestra/sandbox/guard.py (34276d69) for AdaMAS-unit, 2026-11-05. Changed: the Codex launcher
# (sandboxed_codex_config) removed -- agents here run on mini-swe-agent, one confined command at a time
# (adamas_unit.sandbox_env).
"""Sandbox-only rule (user decision 2026-10-11): nothing that starts an agent or runs workspace code may run unconfined.

Three layers, each fail-closed:

1. ``require_sandbox(role)`` before any agent start: the configuration says
   enforced, Landlock ABI >= 4 is available (filesystem and TCP rules), and the
   network guard library builds.
2. ``assert_confined_launch(cmd, env)`` on the exact command about to start: it
   begins with the Landlock launcher, its policy restricts the network (and the
   filesystem, except for sealed held-out scoring), and ``LD_PRELOAD`` carries
   the network guard.
3. The launcher itself (``orchestra.sandbox.exec``) verifies after applying the
   rules and before ``exec`` that a forbidden path really reads as EACCES and
   that the guard is preloaded, and exits 126 otherwise.

``run_confined`` is the one way driver-side code and scripts run agent-written
code (gates, authored suites, held-out scoring, baseline checks).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from orchestra.sandbox import policy as SB

MIN_ABI = 4
_READY: dict[str, Any] = {}


SandboxRequiredError = SB.SandboxRequiredError


def _config_enforced() -> bool:
    return bool((SB.load_config().get("sandbox") or {}).get("enforced"))


def require_sandbox(role: str) -> None:
    """Refuse to start ``role`` unless the sandbox can be in force. Cached per process once it passes."""
    if _READY.get("ok"):
        return
    if not _config_enforced():
        raise SandboxRequiredError(f"{role}: configs/sandbox.yaml must say sandbox.enforced: true; refusing to start")
    from orchestra.sandbox.landlock import abi_version

    try:
        abi = int(abi_version())
    except Exception as exc:  # noqa: BLE001
        raise SandboxRequiredError(f"{role}: Landlock unavailable ({exc}); refusing to start") from exc
    if abi < MIN_ABI:
        raise SandboxRequiredError(f"{role}: Landlock ABI {abi} < {MIN_ABI} (no TCP rules); refusing to start")
    try:
        lib = SB.ensure_netguard()
    except Exception as exc:  # noqa: BLE001
        raise SandboxRequiredError(f"{role}: network guard unavailable ({exc}); refusing to start") from exc
    if not Path(lib).is_file():
        raise SandboxRequiredError(f"{role}: network guard {lib} missing; refusing to start")
    _READY.update(ok=True, abi=abi, netguard=str(lib))


def _policy_of(cmd: list[str]) -> dict[str, Any] | None:
    prefix = SB.launcher(Path("X"))
    if len(cmd) < len(prefix) or cmd[: len(prefix) - 2] != prefix[:-2] or cmd[len(prefix) - 1] != "--":
        return None
    try:
        return json.loads(Path(cmd[len(prefix) - 2]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def assert_confined_launch(cmd: list[str], env: dict[str, str] | None, *, role: str, allow_open_fs: bool = False) -> dict[str, Any]:
    """The command starts with the Landlock launcher under a restrictive policy and the guard is preloaded."""
    require_sandbox(role)
    pol = _policy_of(list(cmd))
    if pol is None:
        raise SandboxRequiredError(f"{role}: command does not start with the sandbox launcher: {' '.join(map(str, cmd[:4]))} ...")
    if not pol.get("restrict_net", True):
        raise SandboxRequiredError(f"{role}: policy leaves the network open")
    if not pol.get("restrict_fs", True) and not allow_open_fs:
        raise SandboxRequiredError(f"{role}: policy leaves the filesystem open (only sealed held-out scoring may)")
    if pol.get("restrict_fs", True) and os.geteuid() == 0 and int((env or {}).get("ADAMAS_SANDBOX_UID") or 0) < 1000:
        raise SandboxRequiredError(f"{role}: the command would run as root (no agent user in its launch environment)")
    pre = (env or {}).get("LD_PRELOAD", "")
    if str(SB.ensure_netguard()) not in pre.split(":"):
        raise SandboxRequiredError(f"{role}: LD_PRELOAD does not carry the network guard")
    return pol


def run_confined(cmd: list[str], *, cwd: str | Path, env: dict[str, str] | None = None, mode: str = "workspace",
                 role: str = "workspace_code", **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` of agent-written code, always confined.

    ``workspace``: file and network rules (the gate's policy: system trees, the
    interpreter's venv, paths named on the command line, ``cwd`` writable).
    ``scoring``: sealed held-out scoring -- file and network rules, the scoring
    user; readable is only ``cwd`` (the scratch copy of the code with this run's
    held-out suite beside it), the system trees and the interpreter.
    """
    base = dict(os.environ if env is None else env)
    if not SB.enforced():                  # only inside a pytest test (policy._test_unconfined); never in a real run
        return subprocess.run(cmd, cwd=str(cwd), env=base, **kwargs)
    if mode == "workspace":
        full, run_env, private = SB.confine_command(list(cmd), cwd=cwd, env=base)
        assert_confined_launch(full, run_env, role=role)
    elif mode == "scoring":
        return _run_scoring(list(cmd), cwd, base, role, kwargs)
    else:
        raise ValueError(f"mode must be workspace or scoring, not {mode!r}")
    try:
        return subprocess.run(full, cwd=str(cwd), env=run_env, **kwargs)
    finally:
        if private is not None:
            SB.clean_private(private)


SCORING_AUDIT_LOG = SB.ROOT / "outputs" / "scoring_audit.jsonl"


def _run_scoring(cmd: list[str], cwd: str | Path, base: dict[str, str], role: str, kwargs: dict[str, Any]) -> subprocess.CompletedProcess:
    """Sealed scoring (user decision 2026-11-19): file rules on, the scoring user, and a record of what the scored
    code tried. The record comes over a pipe the scored code cannot rewind; it is appended to
    ``outputs/scoring_audit.jsonl`` and returned as ``.scoring_audit`` on the result."""
    import threading
    import time

    r, w = os.pipe()
    chunks: list[bytes] = []

    def pump() -> None:
        while True:
            b = os.read(r, 65536)
            if not b:
                return
            if sum(map(len, chunks)) < 4_000_000:
                chunks.append(b)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    private = None
    try:
        full, run_env, private = SB.confine_scoring(cmd, base, cwd=cwd, audit_fd=w)
        assert_confined_launch(full, run_env, role=role)
        p = subprocess.run(full, cwd=str(cwd), env=run_env, pass_fds=(w,), **kwargs)
    finally:
        os.close(w)
        th.join(timeout=10)
        os.close(r)
        if private is not None:
            SB.clean_private(private)
    events = []
    for line in b"".join(chunks).decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("event") in ("permission_change", "read_denied", "list_denied"):
            events.append({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "role": role, "cwd": str(cwd),
                           "event": e["event"], "level": "CRITICAL" if e["event"] == "permission_change" else "WARNING",
                           "target": str(e.get("target"))[:300], "how": str(e.get("how"))[:80]})
    if events:
        SCORING_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SCORING_AUDIT_LOG.open("a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")
    p.scoring_audit = events  # type: ignore[attr-defined]
    return p


__all__ = ["MIN_ABI", "SandboxRequiredError", "assert_confined_launch", "require_sandbox", "run_confined"]
