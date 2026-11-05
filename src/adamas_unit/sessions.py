"""One agent session = one mini-swe-agent run in a confined environment on a ledger lease (USING §4 item 3).

``run_session`` wires a ``BudgetedAgent`` (the ledger, its lease, the planner for top-ups, the summariser for
compressions, the prober for progress) to a ``ConfinedEnvironment`` on the workspace, runs it, saves the trajectory
next to the command capture and closes the lease. The model is passed in, so the positive control can drive a session
with a scripted model and no quota.

Prompts: ``configs/roles/<role>.md`` + ``configs/roles/common.md`` (system); the unit brief and the role's extra text
(instance). Budget numbers in the prompt are the lease's at the start of the session.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adamas_unit.agent import BudgetedAgent
from adamas_unit.ledger import Ledger, Lease
from adamas_unit.sandbox_env import ConfinedEnvironment
from orchestra.sandbox import policy as SB

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "configs" / "roles"


def role_prompt(role: str) -> str:
    own = (ROLES / f"{role}.md").read_text(encoding="utf-8").strip()
    return own + "\n\n" + (ROLES / "common.md").read_text(encoding="utf-8").strip()


def run_session(*, role: str, system: str, instance: str, workspace: Path, venv: Path | None, session_dir: Path,
                ledger: Ledger, lease: Lease, model: Any, planner: Callable[[dict], str] | None = None,
                summarizer: Callable[[list[dict]], tuple[str, int, int]] | None = None,
                prober: Any = None, agent_config: dict[str, Any] | None = None, command_timeout: int = 300,
                manifest_dir: Path | None = None) -> dict[str, Any]:
    """``manifest_dir``: the run directory whose ``sandbox_manifest.json`` gets this session's entry (the breach
    scanner reads the policy and the capture from it)."""
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    capture = session_dir / "commands.jsonl"
    env = ConfinedEnvironment(workspace=str(workspace), role=role, capture=str(capture), venv=str(venv or ""),
                              timeout=command_timeout)
    if manifest_dir is not None:
        SB.write_manifest(Path(manifest_dir), {"kind": "agent", "role": role, "request_id": lease.lease_id,
                                               "policy": env.policy.to_dict(), "capture": str(capture),
                                               "trajectory": str(session_dir / "trajectory.json"), "workspace": str(workspace)})
    if prober is not None:
        prober.start(lease.lease_id, lease.unit, role)
    agent = BudgetedAgent(model, env, ledger=ledger, lease=lease, planner=planner, summarizer=summarizer,
                          progress=prober, system_template=system, instance_template="{{task}}",
                          **(agent_config or {}))
    t0 = time.time()
    try:
        out = agent.run(instance, tokens=lease.tokens, seconds=int(lease.seconds))
    finally:
        env.close()
        agent.save(session_dir / "trajectory.json")
        ledger.close_lease(lease.lease_id)
    rec = {"role": role, "lease": lease.lease_id, "exit_status": out.get("exit_status"), "calls": agent.n_calls,
           "used_tokens": lease.used_tokens, "compressions": agent.compressions, "requests": agent.requests,
           "seconds": round(time.time() - t0, 1), "capture": str(capture)}
    (session_dir / "session.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    return rec
