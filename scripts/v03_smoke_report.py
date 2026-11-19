#!/usr/bin/env python3
"""What happened in one v0.3 round directory (no quota): the events that rewrite the message list, model-call
errors, and whether every saved conversation still pairs each tool call with its result.

    uv run python scripts/v03_smoke_report.py outputs/smoke_v03/<round>/<task>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def pairing(messages: list[dict]) -> list[str]:
    """Problems in a message list: a tool result without its call, or a call without its result before the next
    assistant turn."""
    out, open_ids = [], set()
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            if open_ids:
                out.append(f"message {i}: {len(open_ids)} tool call(s) of the previous turn have no result")
            open_ids = {c.get("id") for c in (m.get("tool_calls") or [])}
        elif role == "tool":
            tid = m.get("tool_call_id")
            if tid not in open_ids:
                out.append(f"message {i}: tool result {tid!r} has no call before it")
            open_ids.discard(tid)
        elif role == "user" and open_ids:
            out.append(f"message {i}: a user message arrives while {len(open_ids)} tool call(s) are unanswered")
            open_ids = set()
    return out


def main() -> int:
    run = Path(sys.argv[1])
    rec = json.loads((run / "round.json").read_text())
    ev = Counter()
    for s in rec["sessions"]:
        for e in s.get("events", []):
            key = e["event"] + (":" + str(e.get("why") or e.get("action") or "") if e["event"] in ("compaction", "planner") else "")
            ev[f"{s.get('kind')}/{key}"] += 1
    traj = {}
    for t in sorted(run.glob("**/trajectory.json")):
        d = json.loads(t.read_text())
        msgs = d.get("messages") or []
        info = d.get("info") or {}
        traj[str(t.parent.relative_to(run))] = {"messages": len(msgs), "pairing_problems": pairing(msgs)[:5],
                                               "exit_status": info.get("exit_status"),
                                               "error": str(info.get("traceback") or info.get("exception_str") or "")[-400:]}
    out = {"exit_status": rec["exit_status"], "resources": rec["resources"], "counts": rec["counts"],
           "sessions": [{"kind": s.get("kind"), "name": s.get("name"), "calls": s["calls"], "steps": s["steps"],
                         "compactions": s["compactions"], "context": s["context"]} for s in rec["sessions"]],
           "events": dict(sorted(ev.items())),
           "planner_fired": [{k: f[k] for k in ("tick", "skill", "action")} for f in rec["planner"]["fired"]],
           "subagents": [{k: s.get(k) for k in ("kind", "exit_status", "quota", "used", "code_changed", "wrote_memory", "reverted")}
                         for s in rec["subagents"]],
           "stages": [{k: s.get(k) for k in ("index", "allocation", "used", "declared_done", "below_minimum", "why")} for s in rec["stages"]],
           "measures": rec["measures"], "trajectories": traj,
           "ledger_side": rec["ledger"].get("side"), "wall_seconds": rec["wall_seconds"]}
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
