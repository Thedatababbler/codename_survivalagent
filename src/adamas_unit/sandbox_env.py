"""The mini-swe-agent environment of AdaMAS-unit: every agent command runs confined (docs/USING_ADAMAS.md §3.3).

mini-swe-agent's agent loop (model calls) runs in the driver process; the only thing it does to the machine is
``Environment.execute(action)``, one bash command at a time. Here each command starts under the Landlock launcher with
the *agent* policy copied from AdaMAS (``orchestra.sandbox.policy.agent_policy``: system trees, the task venv, the
workspace writable, a private HOME/TMPDIR; no path is granted for being named on the command line) and the network
guard preloaded; ``assert_confined_launch`` checks the exact command before it starts. Without the sandbox nothing
runs (``require_sandbox``).

Every command, its output and exit code are appended to ``<capture>`` (JSONL) for the breach scanner.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from minisweagent.exceptions import Submitted
from pydantic import BaseModel

from orchestra.sandbox import policy as SB
from orchestra.sandbox.guard import assert_confined_launch, require_sandbox

SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


class WorkspaceAccessError(RuntimeError):
    """The agent cannot use its own repository as its session requires: the run stops, nothing is diagnosed by an agent."""


class ConfinedEnvironmentConfig(BaseModel):
    workspace: str
    role: str
    capture: str                    # JSONL file of every command (breach scanner input)
    venv: str = ""                  # the task venv: `python` / `pytest` resolve to it
    timeout: int = 120              # per command, seconds; the driver lowers it to the time left (DESIGN §5.4)


class ConfinedEnvironment:
    def __init__(self, *, config_class: type = ConfinedEnvironmentConfig, **kwargs):
        self.config = config_class(**kwargs)
        require_sandbox(f"agent {self.config.role}")
        self.private = Path(tempfile.mkdtemp(prefix=f"unit-{self.config.role}-"))
        Path(self.config.capture).parent.mkdir(parents=True, exist_ok=True)
        venv = self.config.venv or None
        pol = SB.agent_policy(workspace=self.config.workspace, private=self.private, venv=venv)
        self.policy = pol
        self.policy_path = SB.write_policy(pol, self.private / "policy.json")
        env = SB.launcher_env(SB.private_env(self.private, base={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}))
        if venv:
            env["PATH"] = f"{venv}/bin" + os.pathsep + env["PATH"]
        env["PYTEST_ADDOPTS"] = f"--rootdir={self.config.workspace} --confcutdir={self.config.workspace} -p no:cacheprovider"
        self.env = env
        SB.grant(self.private)
        self.preflight()

    def preflight(self, *, writable: bool = True) -> None:
        """Before the agent's first command (user decision 2026-11-19): the program itself confirms, through the
        agent's own launcher, that the agent can read its repository (and write it, unless it is a read-only
        session). If not, the run stops here with an error; an agent is never left to diagnose permissions."""
        ws = self.config.workspace
        SB.grant(ws)
        probe = ".adamas_preflight"
        cmd = "ls -A . >/dev/null || exit 11; " + (
            f"echo ok > {probe} && cat {probe} >/dev/null && rm -f {probe} || exit 12" if writable
            else f"if ( echo x > {probe} ) 2>/dev/null; then rm -f {probe}; exit 13; fi")
        full = [*SB.launcher(self.policy_path), "/bin/bash", "-c", cmd]
        assert_confined_launch(full, self.env, role=self.config.role)
        try:
            rc, out = _run(full, ws, self.env, 60)
        except subprocess.TimeoutExpired:
            rc, out = -1, "timed out"
        if rc != 0:
            what = {11: "cannot read", 12: "cannot write", 13: "can write (it must be read-only)"}.get(rc, f"check failed (exit {rc})")
            raise WorkspaceAccessError(f"{self.config.role}: the agent {what} its repository {ws}: {out.strip()[-300:]}")

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        full = [*SB.launcher(self.policy_path), "/bin/bash", "-c", command]
        assert_confined_launch(full, self.env, role=self.config.role)
        SB.grant(self.config.workspace)          # files the driver wrote since (the memory file, a restored copy)
        t0 = time.time()
        try:
            rc, out = _run(full, cwd or self.config.workspace, self.env, timeout or self.config.timeout)
            output = {"output": out, "returncode": rc, "exception_info": ""}
        except subprocess.TimeoutExpired as e:
            out = e.output if isinstance(e.output, str) else ""
            output = {"output": out, "returncode": -1, "exception_info": f"The command timed out after {e.timeout} s.",
                      "extra": {"exception_type": "TimeoutExpired"}}
        with open(self.config.capture, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": self.config.role, "command": command, "output": output["output"][-20000:],
                                 "exit_code": output["returncode"], "seconds": round(time.time() - t0, 2)}) + "\n")
        lines = output["output"].lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == SUBMIT_MARKER and output["returncode"] == 0:
            raise Submitted({"role": "exit", "content": "".join(lines[1:]),
                             "extra": {"exit_status": "Submitted", "submission": "".join(lines[1:])}})
        return output

    def close(self) -> None:
        SB.clean_private(self.private)

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        # unlike LocalEnvironment, never the driver's os.environ (it holds the model channel's key)
        return {"workspace": self.config.workspace, "role": self.config.role, **kwargs}

    def serialize(self) -> dict:
        return {"info": {"config": {"environment": self.config.model_dump(mode="json"),
                                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}"}}}


def _run(cmd: list[str], cwd: str, env: dict[str, str], timeout: int) -> tuple[int, str]:
    """Run, kill the whole process group on timeout (as mini-swe-agent's LocalEnvironment)."""
    p = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=out) from None
    return p.returncode, out


def remove_tree(path: str | Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
