"""One ContextBench run on the v0.3 executor (docs/CONTEXTBENCH.md §5).

Layout (``outputs/cb/<round>/<run id>/``; the run id is random: neither the directory nor anything in the working
directory names the task's seed, level or instance):

    ws/                           the agent's working directory (empty at the start; the memory file for arms with skills)
    executor/                     commands.jsonl, trajectory.json (breach scanner input)
    agent/context_snapshots/      turn-NNNN.json, written by ContextBench's record_snapshot (the graders' input)
    agent/interactive_stream.json written by ContextBench's TurnSource
    ledger.json, sandbox_manifest.json
    result.json                   score, how the run ended, usage, counts, the planner's actions
    digest.json                   what an evolver may read: figures only

``outputs/cb/<round>/index.jsonl`` maps run ids to instances (the driver's; outside every working directory).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestra.sandbox import policy as SB
from survival import cb as CB
from survival import memory as MEM
from survival.agents import WriteToken
from survival.cb_agent import ARMS, MEMORY_TEMPLATE, StreamAgent, StreamEnvironmentMixin
from survival.ledger import Limits, RoundLedger
from survival.planner import Planner
from survival.round import OwnedEnvironment
from survival.skills import SkillSet

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "configs" / "roles_cb"
OUT = ROOT / "outputs" / "cb"
CONTEXT_SKILLS = ("memory-compaction", "token-reminder", "subagent-handoff")

# how a session that the stream did not end is recorded in ContextBench's terms
ENDINGS = {"ContextExhausted": "budget_wall", "RollbackLimit": "budget_wall", "RoundBudgetExhausted": "max_steps",
           "TokensExhausted": "max_steps", "StepLimit": "max_steps", "CallCap": "max_steps", "RepeatedFormatError": "max_steps"}


class StreamEnvironment(StreamEnvironmentMixin, OwnedEnvironment):
    pass


@dataclass
class RunConfig:
    tokens: int                          # the run's cap (prompt tokens as the server reports them)
    seconds: float = 21600.0
    compressions: int = 400              # the pool: a safety limit, far above what a run needs
    max_rollbacks: int = 6
    rollback_limit: int = 20
    keep_recent: int = 6
    command_timeout: int = 120
    max_output_chars: int = 50_000       # a whole operation fits in one tool output (ContextBench's own setting)
    steps_per_op: int = 8                # the step limit is steps_per_op x operations + 60


def role(name: str) -> str:
    return (ROLES / f"{name}.md").read_text(encoding="utf-8").strip()


def system_text(arm: str, skills: SkillSet | None) -> str:
    guide = ""
    if skills is not None:
        guide = "\n## Reading the resource figures\n\n" + "\n\n".join(
            f"### {n}\n{skills[n].section('Reading the readout')}" for n in CONTEXT_SKILLS[:2]) + "\n"
    return (role("system").replace("{{management_text}}", role("management_none" if arm == "none" else "management_skills"))
            .replace("{{readout_guidance}}", guide).replace("{{marker_paragraph}}", "\n" + CB.marker_paragraph()))


def make_summarizer(complete: Callable[[list[dict]], tuple[str, int, int]], instruction: str):
    prompt = role("summary")

    def summarize(messages: list[dict]) -> tuple[str, int, int]:
        parts = []
        for m in CB.wire(messages):
            text = str(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                text += f"\n[command] {(tc.get('function') or {}).get('arguments', '')}"
            parts.append(f"## {m.get('role')}\n{text}")
        return complete([{"role": "system", "content": prompt},
                         {"role": "user", "content": f"# The agent's task\n\n{instruction}\n\n# The conversation to replace\n\n" + "\n\n".join(parts)}])
    return summarize


def digest(result: dict[str, Any]) -> dict[str, Any]:
    """What an evolver may read of a run: figures and fixed words only -- no operation text, no reply, no answer,
    no seed."""
    r = result
    keep_rewards = {k: v for k, v in r["rewards"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return {"family": r["family"], "level": r["level"], "arm": r["arm"], "skills": r["skills"],
            "reward": r["reward"], "rewards": keep_rewards, "graded_from": r["graded_from"],
            "terminated_by": r["terminated_by"], "exit_status": r["exit_status"],
            "ops_delivered": r["ops_delivered"], "ops_total": r["ops_total"],
            "tokens": r["tokens"], "counts": r["counts"], "context": r["context"],
            "planner_fired": [{"skill": f["skill"], "action": f["action"], "context": f["state"]["context"], "tick": f["tick"]}
                              for f in r["planner_fired"]],
            "events": [{k: e[k] for k in ("event", "call", "ops", "why", "from_memory", "dropped") if k in e} for e in r["events"]],
            "trace": [[t["call"], t["ops"], round(t["context"] / r["context"]["window"], 3)] for t in r["trace"]]}


def run(family: str, level: str, seed: int, *, arm: str, round_id: str, cfg: RunConfig, agent_model: Callable[[], Any],
        complete: Callable[[list[dict]], tuple[str, int, int]] | None = None, skills: SkillSet | None = None,
        skills_id: str = "", out_root: Path = OUT, rep: int = 0) -> dict[str, Any]:
    if arm not in ARMS:
        raise ValueError(arm)
    if (skills is None) != (arm == "none"):
        raise ValueError("the arm `none` runs without skills; every other arm needs a skill set")
    CB.ensure()
    inst = CB.instance(family, level, seed)
    run_id = "r" + uuid.uuid4().hex[:12]
    run_dir = (Path(out_root) / round_id / run_id).resolve()
    run_dir.mkdir(parents=True)
    with (run_dir.parent / "index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run": run_id, "family": family, "level": level, "seed": int(seed), "arm": arm,
                             "skills": skills_id, "rep": rep}) + "\n")
    ws, sdir, logs = run_dir / "ws", run_dir / "executor", run_dir / "agent"
    ws.mkdir()
    if arm != "none":
        (ws / MEM.MEMORY_FILE).write_text(MEMORY_TEMPLATE, encoding="utf-8")
    t0 = time.time()
    records = inst.records()
    ledger = RoundLedger(Limits(tokens=cfg.tokens, seconds=cfg.seconds, compressions=cfg.compressions, reviews=0, repairs=0,
                                workers=0, subagents=0), 1, path=run_dir / "ledger.json")
    ledger.open_stage(0)
    planner = Planner(skills, ledger) if skills is not None else None
    token = WriteToken()
    capture = sdir / "commands.jsonl"
    sdir.mkdir()
    env = StreamEnvironment(token=token, owner="executor", workspace=str(ws), role="executor", capture=str(capture),
                            venv="", timeout=cfg.command_timeout)
    SB.write_manifest(run_dir, {"kind": "agent", "role": "executor", "request_id": "executor", "policy": env.policy.to_dict(),
                                "capture": str(capture), "trajectory": str(sdir / "trajectory.json"), "workspace": str(ws)})
    token.take("executor")
    source = CB.turn_source(inst, logs)
    instruction = inst.instruction()
    max_steps = cfg.steps_per_op * len(records) + 60
    agent = StreamAgent(agent_model(), env, source=source, logs_dir=logs, arm=arm, planner=planner, ledger=ledger,
                        name="executor", ws=ws, token=token,
                        summarizer=make_summarizer(complete, instruction) if (complete and arm != "none") else None,
                        system_template="{{system_text}}", instance_template="{{task}}", context_window=CB.usable(),
                        max_rollbacks=cfg.max_rollbacks, rollback_limit=cfg.rollback_limit, keep_recent=cfg.keep_recent,
                        max_steps=max_steps, max_output_chars=cfg.max_output_chars)
    out: dict[str, Any] = {}
    crashed = ""
    try:
        out = agent.run(instruction, system_text=system_text(arm, skills))
    except BaseException as e:  # noqa: BLE001 - recorded, then raised again
        crashed = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        env.close()
        agent.save(sdir / "trajectory.json")
        try:
            agent.final_snapshot()                      # needle_retention is graded on this one
        finally:
            status = str(out.get("exit_status") or ("Crashed" if crashed else ""))
            if not source.done and status in ENDINGS:   # a crash leaves no terminal reason (it is not a result)
                source.terminate(ENDINGS[status])
            source.write()
    rewards = CB.grade(run_dir, inst)
    led = ledger.snapshot()
    charges = [e for e in ledger.events if e["event"] == "charge"]
    rec = agent.record()
    fired = planner.fired if planner is not None else []
    result = {"run": run_id, "round": round_id, "family": family, "level": level, "seed": int(seed), "arm": arm,
              "skills": skills_id, "rep": rep, "instance": inst.meta(),
              "reward": float(rewards.get("reward", 0.0)), "rewards": rewards, "graded_from": rewards.get("graded_from"),
              "grader_valid": rewards.get("grader_valid"),
              "terminated_by": source.terminated_by, "exit_status": status,
              "ops_delivered": source.n_delivered, "ops_total": source.n_total,
              "tokens": {"prompt": led["used"], "completion": sum(e["completion_tokens"] for e in charges),
                         "summary_prompt": sum(e["prompt_tokens"] for e in charges if e["what"] == "compression"),
                         "cap": cfg.tokens},
              "counts": {"calls": rec["calls"], "steps": rec["steps"], "free_turns": rec["free_turns"],
                         "format_hints": rec["format_hints"], "compactions": rec["compactions"],
                         "compactions_from_memory": sum(1 for e in rec["events"] if e.get("event") == "compaction" and e.get("from_memory")),
                         "rollbacks": led["rollbacks"], "truncations": rec["truncations"],
                         "reminders": sum(1 for f in fired if f["action"] == "remind"),
                         "refusals": source.n_refusals, "explains": source.n_explains, "snapshots": rec["snapshots"]},
              "context": {"window": CB.usable(), "peak_o200k": rec["context"]["peak_o200k"], "peak_served": rec["context"]["peak"],
                          "served_per_o200k": rec["context"]["served_per_o200k"]},
              "planner_fired": fired, "events": rec["events"], "trace": rec["trace"],
              "config": cfg.__dict__, "contextbench_commit": CB.config()["commit"], "wall_seconds": round(time.time() - t0, 1)}
    (run_dir / "result.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    (run_dir / "digest.json").write_text(json.dumps(digest(result), indent=1, default=str), encoding="utf-8")
    return result
