#!/usr/bin/env python3
"""Sandbox self-check of AdaMAS-unit (no model call, no quota).

1. ``require_sandbox``: config enforced, Landlock ABI >= 4, network guard builds.
2. AdaMAS's probe set (``policy.self_check``) under the agent policy: every forbidden path reads as denied, the external
   network is refused, the model proxy port connects.
3. ``ConfinedEnvironment`` end to end: a command writes in its workspace, cannot read AdaMAS / AdaMAS-single / this
   project's source or outputs, cannot reach the internet, and every command is captured.

    uv run python scripts/sandbox_selfcheck.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.sandbox_env import ConfinedEnvironment, remove_tree  # noqa: E402
from orchestra.sandbox import policy as SB  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402


def main() -> int:
    require_sandbox("sandbox self-check")
    out_dir = ROOT / "outputs" / "sandbox"
    out_dir.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix="selfcheck-ws-", dir=out_dir))
    priv = Path(tempfile.mkdtemp(prefix="selfcheck-priv-"))
    checks: dict[str, bool] = {}
    try:
        pol = SB.agent_policy(workspace=ws, private=priv, venv=None)
        r = SB.self_check(pol, SB.private_env(priv), SB.default_probes())
        checks["probes: forbidden reads denied, internet refused, proxy reachable"] = bool(r["ok"])
        cap = ws.parent / f"{ws.name}.commands.jsonl"
        env = ConfinedEnvironment(workspace=str(ws), role="selfcheck", capture=str(cap))
        try:
            def run(c: str) -> dict:
                return env.execute({"command": c})
            checks["writes inside the workspace"] = run("echo ok > f.txt && cat f.txt")["output"].strip() == "ok"
            for name, path in [("AdaMAS source", "/root/projects/AdaMAS/src/orchestra/__init__.py"),
                               ("AdaMAS-single outputs", "/root/projects/AdaMAS-single/outputs"),
                               ("this project's source", str(ROOT / "src")),
                               ("this project's configs", str(ROOT / "configs" / "sandbox.yaml")),
                               ("global /tmp", "/tmp"), ("sealed archive", "/root/.adamas_sealed")]:
                # a file is read with cat, a directory listed with ls (stat of a path is not a read; Landlock allows it)
                res = run(f"cat {path} 2>&1" if Path(path).is_file() else f"ls {path} 2>&1")
                checks[f"cannot read {name}"] = res["returncode"] != 0 and "denied" in res["output"].lower()
            res = run("python3 -c \"import socket;socket.create_connection(('1.1.1.1',443),timeout=5)\" 2>&1")
            checks["internet refused"] = res["returncode"] != 0
            res = run("cd .. && ls 2>&1")
            checks["cannot list the workspace's parent"] = res["returncode"] != 0
            n = len(cap.read_text().splitlines())
            checks["every command captured"] = n == 9          # 1 write + 6 reads + internet + parent listing
        finally:
            env.close()
    finally:
        remove_tree(ws)
        remove_tree(priv)
        for f in out_dir.glob(f"{ws.name}.commands.jsonl"):
            f.unlink()
    for k, v in checks.items():
        print(("ok  " if v else "FAIL"), k)
    ok = all(checks.values())
    print(json.dumps({"ok": ok, "passed": sum(checks.values()), "checks": len(checks)}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
