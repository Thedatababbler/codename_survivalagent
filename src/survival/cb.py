"""ContextBench, used read-only (docs/CONTEXTBENCH.md; Apache-2.0, https://github.com/RulinShao/ContextBench).

This module is the only place that imports ``contextbench``. The checkout stays outside the project and is never
modified: its generators, instruction scaffolds, turn protocol (``TurnSource``), snapshot writer and graders are
called as they are. Before the first use the checkout is checked against the pinned commit (``configs/contextbench.yaml``)
and must be clean; otherwise nothing runs.

Instances live in the sealed directory (``sealed/contextbench/instances/<family>-<level>-seed<N>/``: ``chunks.jsonl``,
``gold.json``, ``meta.json`` written by ContextBench's generator, plus ``instruction.md`` and ``feasibility.json``).
Only the driver reads them; ``gold.json`` is opened by ``grade`` alone, after a run has ended.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "contextbench.yaml"
FAMILIES = ("kv_store", "log_triage", "needle_retention", "sudoku_sketchpad")
_READY = False


class ContextBenchError(RuntimeError):
    pass


def config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["contextbench"]


def checkout() -> Path:
    return Path(config()["path"])


def ensure() -> None:
    """The pinned, unmodified checkout on the import path (once per process)."""
    global _READY
    if _READY:
        return
    cfg = config()
    path = Path(cfg["path"])
    if not (path / "contextbench" / "__init__.py").is_file():
        raise ContextBenchError(f"{path}: no ContextBench checkout")
    git = ["git", "-c", f"safe.directory={path}", "-C", str(path)]
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=60).stdout.strip()
    if head != cfg["commit"]:
        raise ContextBenchError(f"{path} is at {head[:12] or '?'}, not at the pinned {cfg['commit'][:12]}")
    dirty = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True, timeout=120).stdout.strip()
    if dirty:
        raise ContextBenchError(f"{path} has local changes; ContextBench is used unmodified")
    sys.dont_write_bytecode = True                      # nothing is written into the checkout
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import contextbench  # noqa: F401
    from contextbench import tokens

    if not tokens.encoder_name().startswith("tiktoken:o200k_base"):
        raise ContextBenchError(f"token counts are {tokens.encoder_name()}, not exact o200k_base")
    _READY = True


def budget() -> tuple[int, int]:
    cfg = config()
    return int(cfg["budget_tokens"]), int(cfg["reserve_tokens"])


def usable() -> int:
    b, r = budget()
    return b - r


# ---------------------------------------------------------------------------------------------- counting, snapshots

def wire(messages: list[dict]) -> list[dict]:
    """The messages as the model receives them (and as a snapshot stores them): role, content, tool calls. The
    session's ``exit`` message and mini-swe-agent's ``extra`` are not part of the context. A scripted model's
    command (no ``tool_calls``) is written as one, so that it is counted like a real call's."""
    out = []
    for m in messages:
        if m.get("role") == "exit":
            continue
        w = {k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id", "reasoning_content") if m.get(k) is not None}
        if "content" not in w:
            w["content"] = ""
        if m.get("role") == "assistant" and not m.get("tool_calls"):
            acts = (m.get("extra") or {}).get("actions") or []
            if acts:
                w["tool_calls"] = [{"type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": a.get("command", "")})}}
                                   for a in acts]
        out.append(w)
    return out


def count(messages: list[dict]) -> int:
    """ContextBench's own count of a context (exact o200k_base, uncalibrated): the ruler of its budget wall."""
    ensure()
    from contextbench.tokens import count_tokens

    return count_tokens(wire(messages))[0]


def snapshot(logs_dir: Path, step: int, messages: list[dict], extra: dict[str, Any]) -> None:
    """``<logs_dir>/context_snapshots/turn-NNNN.json``, written by ContextBench's ``record_snapshot``."""
    ensure()
    from contextbench.tokens import record_snapshot

    if record_snapshot(logs_dir, step, wire(messages), extra) is None:
        raise ContextBenchError(f"snapshot {step} could not be written under {logs_dir}")


# ---------------------------------------------------------------------------------------------- instances

@dataclass(frozen=True)
class Instance:
    family: str
    level: str
    seed: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    def records(self) -> list[dict]:
        return [json.loads(x) for x in (self.path / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]

    def instruction(self) -> str:
        return (self.path / "instruction.md").read_text(encoding="utf-8")

    def meta(self) -> dict:
        return json.loads((self.path / "feasibility.json").read_text(encoding="utf-8"))


def sealed_dir() -> Path:
    d = ROOT / config()["sealed_dir"] / "instances"
    if not d.is_dir():
        for part in reversed([d, *d.parents][: len(d.relative_to(ROOT).parts)]):
            if not part.exists():       # new directories of this project, each made 0700; nothing existing is changed
                part.mkdir(mode=0o700)
    return d


def check_seed(seed: int) -> None:
    cfg = config()
    if int(seed) in set(cfg["forbidden_seeds"]) or int(seed) < int(cfg["min_seed"]):
        raise ContextBenchError(f"seed {seed}: seeds start at {cfg['min_seed']}; the published seeds are never used")


def check_level(family: str, level: str) -> None:
    allowed = config()["levels"].get(family)
    if allowed is None or level not in allowed:
        raise ContextBenchError(f"{family} {level}: not one of the levels in use ({', '.join(allowed or [])})")


def instance(family: str, level: str, seed: int, *, generate: bool = True) -> Instance:
    """The sealed instance, generated on first use by ContextBench's generator with the level's knobs of record."""
    check_seed(seed)
    check_level(family, level)
    d = sealed_dir() / f"{family}-{level}-seed{int(seed)}"
    inst = Instance(family, level, int(seed), d)
    if (d / "feasibility.json").is_file():
        return inst
    if not generate:
        raise ContextBenchError(f"{d.name}: not generated")
    ensure()
    from contextbench import feasibility
    from contextbench.cli import level_entry, load_config, resolve_level
    from contextbench.levels import level_regime
    from contextbench.tokens import count_text

    mod = importlib.import_module(f"contextbench.tasks.{family}.generate")
    levels = load_config()
    result = mod.generate(**resolve_level(levels, family, level), seed=int(seed))
    d.mkdir(mode=0o700, exist_ok=True)
    mod.write_outputs(d, result)
    gold = json.loads((d / "gold.json").read_text(encoding="utf-8"))
    (d / "instruction.md").write_text(mod.instruction(gold, delivery="bash"), encoding="utf-8")
    b, r = budget()
    verdict = feasibility.evaluate(d, b, r, int(config()["prompt_tokens"]), level_regime(level_entry(levels, family, level)))
    if not verdict.get("feasible"):
        raise ContextBenchError(f"{d.name}: not feasible at budget {b}: {feasibility.format_verdict(verdict)}")
    recs = inst.records()
    toks = [count_text(x.get("body", "")) for x in recs]
    (d / "feasibility.json").write_text(json.dumps({
        "family": family, "level": level, "seed": int(seed), "n_ops": len(recs), "stream_tokens": sum(toks),
        "max_op_tokens": max(toks), "pressure": round(sum(toks) / b, 3), "budget": b, "reserve": r,
        "feasible": True, "commit": config()["commit"]}, indent=1), encoding="utf-8")
    return inst


def turn_source(inst: Instance, logs_dir: Path):
    """ContextBench's own delivery object for this instance's stream (imports nothing from Harbor)."""
    ensure()
    from contextbench.harbor.turn_source import TurnSource

    return TurnSource.from_records(inst.records(), logs_dir=logs_dir, identity={"path": "(held by the driver)", "removed": True})


def marker_paragraph() -> str:
    ensure()
    from contextbench import stream

    return stream.MARKER_PARAGRAPH


def texts() -> dict[str, str]:
    ensure()
    from contextbench import stream

    return {"advance": stream.ADVANCE_MARKER, "submit": stream.SUBMIT_MARKER, "explain": stream.EXPLAIN_NON_STANDALONE_TEXT,
            "no_tool_call": ("(No tool call in your last turn. Only a `bash` command can advance the task: when you have "
                             f"handled the current operation, run `echo {stream.ADVANCE_MARKER}`.)")}


def mentions_advance(text: str) -> bool:
    ensure()
    from contextbench import stream

    return stream.mentions_advance_marker(text)


def grade(run_dir: Path, inst: Instance) -> dict[str, Any]:
    """ContextBench's grader on the run's snapshots. The only reader of ``gold.json``."""
    ensure()
    from contextbench.graders import grade_run

    return grade_run(run_dir, json.loads((inst.path / "gold.json").read_text(encoding="utf-8")))
