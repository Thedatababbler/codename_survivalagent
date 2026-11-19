"""The sessions of a v0.3 round, on mini-swe-agent's ``DefaultAgent`` (refactor §2, §3).

``ManagedAgent`` is what every session is (executor and sub-agents alike): each model call is booked on the ledger,
each tool result ends with the resource readout, and a turn that would overflow the context window is undone and
retried after a compaction (``context.ContextWindow``). ``ExecutorAgent`` adds what only the executor has: the
stages of the frozen plan, the planner's triggers before every call, and the management commands

    stage_done                                         declare the current stage complete
    request_budget share=<0..1>|tokens=<N> reason="…"  ask for a top-up of this stage
    subagent type=review|repair|worker budget=<0..1>|tokens=<N> task="…"
    compact                                            compact the context now (from the memory file when it is fresh)

Management commands never reach the shell and do not count towards the step limit; neither does a command that
only rewrites the memory file. Their tokens are charged as any other call's (§3.3).

Only one session writes at a time: a sub-agent runs inside the executor's ``subagent`` command, and every
environment checks that its session holds the write token before it runs a command (``WriteToken``).
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.exceptions import FormatError, LimitsExceeded

from adamas_unit.agent import usage_of
from adamas_unit.sandbox_env import SUBMIT_MARKER
from survival import memory as MEM
from survival.context import ContextWindow, truncate_output
from survival.ledger import Refused, RoundLedger
from survival.planner import Planner, build_state, readout

MANAGEMENT = re.compile(r"^\s*(stage_done|request_budget|subagent|compact)\b(.*)$", re.S)
TRAILING = re.compile(r"^\s*(stage_done|request_budget|subagent|compact)\b([^\n]*)$")


class ManagedConfig(AgentConfig):
    context_window: int = 64_000
    max_rollbacks: int = 6
    rollback_limit: int = 20             # roll-backs of one session in all: at this many the session ends (no endless loop)
    keep_recent: int = 6
    max_output_chars: int = 10_000
    max_steps: int = 300                 # counted steps (management actions are free)
    call_cap: int = 0                    # all model calls; 0: 2 x max_steps + 24
    reserve_tokens: int = 4_000          # room a call needs beyond the context it sends
    step_limit: int = 0
    cost_limit: float = 0.0


class WriteToken:
    """Who may run commands now. Exactly one holder; a sub-agent borrows it from the executor and gives it back."""

    def __init__(self) -> None:
        self.holder: str | None = None
        self.log: list[tuple[float, str, str]] = []

    def take(self, who: str) -> str | None:
        prev, self.holder = self.holder, who
        self.log.append((time.time(), "take", who))
        return prev

    def give_back(self, prev: str | None) -> None:
        self.log.append((time.time(), "back", str(prev)))
        self.holder = prev

    def check(self, who: str) -> None:
        if self.holder != who:
            raise RuntimeError(f"{who} ran a command while {self.holder} holds the write token")


def _stop(status: str, text: str = "") -> LimitsExceeded:
    return LimitsExceeded({"role": "exit", "content": text or status, "extra": {"exit_status": status, "submission": text}})


def parse_kv(args: str) -> dict[str, str]:
    """``key=value`` pairs; a value may be quoted; ``task=`` and ``reason=`` take the rest of the text."""
    out: dict[str, str] = {}
    m = re.search(r"\b(task|reason)=(.*)$", args, re.S)
    if m:
        out[m.group(1)] = m.group(2).strip().strip("\"'")
        args = args[:m.start()]
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = args.split()
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class ManagedAgent(DefaultAgent):
    """A session with the ledger, the readout and the context window. ``quota_id``: a sub-agent's quota (None: the
    executor, charged on the current stage)."""

    def __init__(self, model, env, *, ledger: RoundLedger, name: str, ws: Path, token: WriteToken,
                 quota_id: str | None = None, summarizer: Callable[[list[dict]], tuple[str, int, int]] | None = None,
                 state_fn: Callable[["ManagedAgent"], dict[str, Any]] | None = None, **kwargs):
        super().__init__(model, env, config_class=ManagedConfig, **kwargs)
        self.ledger, self.name, self.ws, self.token, self.quota_id = ledger, name, Path(ws), token, quota_id
        self.summarizer, self.state_fn = summarizer, state_fn
        self.window = ContextWindow(self.config.context_window, max_retries=self.config.max_rollbacks)
        self.steps = 0
        self.free_turns = 0
        self.compactions = 0
        self.tracker = MEM.MemoryTracker(self.ws)
        self.events: list[dict[str, Any]] = []
        self.no_submit = ""              # the executor has no submit command (it declares stages); the text it gets

    # ------------------------------------------------------------------ what the subclasses change

    def tokens_left(self) -> int:
        return self.ledger.quotas[self.quota_id].left()

    def before_call(self) -> None:
        """Sub-agents: the framework compacts at nine tenths of the window while the pool lasts."""
        if self.window.share(self.messages) >= 0.9:
            self.compact(use_memory=False, why="framework")

    def out_of_tokens(self) -> None:
        raise _stop("TokensExhausted", "the quota is used up")

    def handle_management(self, verb: str, args: str) -> dict[str, Any] | None:
        """Sub-agents have one management command: ``compact``."""
        if verb != "compact":
            return None
        text = self._compact(args)
        return {"replaced": True} if text is None else {"output": text, "returncode": 0, "exception_info": ""}

    def compact_keep(self) -> int:
        return self.config.keep_recent

    def _compact(self, args: str) -> str | None:
        pending = self.messages.pop()                   # the turn that asked: it has no result to keep
        if not self.compact(use_memory=True, why="asked", keep_recent=self.compact_keep()):
            self.messages.append(pending)
            return "compact: nothing was compacted (no compaction left in the pool, or nothing old enough)."
        self.messages.append({"role": "user", "content": "[compact: done]\n" + self.readout_line()})
        return None

    def state(self) -> dict[str, Any]:
        return self.state_fn(self) if self.state_fn else {}

    def readout_line(self) -> str:
        st = self.state()
        q = self.ledger.quotas[self.quota_id]
        return readout(st, self.ledger.current, self.ledger.n_stages, grant_tokens=min(1.0, q.used / q.tokens) if q.tokens else 1.0)

    # ------------------------------------------------------------------ the loop

    def call_cap(self) -> int:
        return self.config.call_cap or 2 * self.config.max_steps + 24

    def query(self) -> dict:
        if self.ledger.exhausted():
            raise _stop("RoundBudgetExhausted", f"the round's {self.ledger.exhausted()} budget is used up")
        if self.steps >= self.config.max_steps:
            raise _stop("StepLimit", "the step limit is reached")
        if self.n_calls >= self.call_cap():
            raise _stop("CallCap", "the model-call cap is reached")
        self.before_call()
        if self.window.over(self.messages):
            self._overflow()
        need = self.window.count(self.messages) + self.config.reserve_tokens
        if self.tokens_left() < need:
            self.out_of_tokens()
            if self.tokens_left() < self.window.count(self.messages) + self.config.reserve_tokens:
                raise _stop("TokensExhausted", "no tokens left for another call")
        self.n_calls += 1
        sent = list(self.messages)
        try:
            message = self.model.query(self.messages)
        except FormatError as e:
            p, c = usage_of(e.messages[0])
            self.ledger.charge(p, c, quota_id=self.quota_id, what="format_error")
            self.window.calibrate(sent, p)
            raise
        p, c = usage_of(message)
        self.ledger.charge(p, c, quota_id=self.quota_id)
        self.window.calibrate(sent, p)
        self.add_messages(message)
        return message

    def _overflow(self) -> None:
        """The next request would not fit the window: undo the newest turns and ask for a compaction (§3.2). Once
        the retries are used up a compaction is forced first; whatever is still over the window after it is undone
        as well (smoke forced-2: a session must not end because one result was too large)."""
        if self.window.retries >= self.config.rollback_limit:
            self.events.append({"event": "rollback_limit", "call": self.n_calls, "rollbacks": self.window.retries})
            self.ledger.note("rollback_limit", session=self.name, rollbacks=self.window.retries)
            raise _stop("RollbackLimit", f"the context overflowed {self.window.retries} times in this session")
        if not self.window.can_retry():
            any(self.compact(use_memory=True, why="forced after the last retry", keep_recent=k)
                for k in (self.compact_keep(), 2, 1))
            if not self.window.over(self.messages):
                return
        before = self.window.count(self.messages)
        dropped, after = self.window.roll_back(self.messages)
        self.window.pin_note(self.messages, self.window.rollback_text(len(dropped)))
        # a planner message fires once per crossing: undone with the turns, it would never come again
        kept = [m for m in dropped if m.get("role") == "user" and str(m.get("content") or "").startswith("[planner:")]
        self.messages.extend(kept)
        self.ledger.note_rollback(session=self.name, dropped=len(dropped), before=before, after=after,
                                  retry=self.window.retries)
        self.events.append({"event": "rollback", "call": self.n_calls, "dropped": len(dropped)})
        if self.window.over(self.messages):
            raise _stop("ContextExhausted", "the context is over the window with nothing left to undo or compact")

    def execute_actions(self, message: dict) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        outputs: list[dict[str, Any]] = []
        counted = False
        for action in actions:
            cmd = str(action.get("command") or "")
            m = MANAGEMENT.match(cmd)
            pre = ""
            if not m:
                # a management command written as the last line of a shell script (smoke forced-2: the memory file
                # rewritten and `compact` in one command -> "compact: command not found"): the shell part runs
                # first, then the management command
                head, _, last = cmd.rstrip().rpartition("\n")
                m2 = TRAILING.match(last)
                if head.strip() and m2:
                    self.token.check(self.name)
                    left = int(max(1, self.ledger.seconds_left()))
                    o = self.env.execute({**action, "command": head}, timeout=min(left, getattr(self.env.config, "timeout", left)))
                    pre = truncate_output(str(o.get("output") or ""), self.config.max_output_chars).rstrip()
                    code_changed, mem_changed = self.tracker.observe()
                    counted = counted or code_changed or not mem_changed
                    self.after_command(code_changed)
                    m, cmd = m2, last
            out = self.handle_management(m.group(1), m.group(2)) if m else None
            if out is not None and pre and not out.get("replaced"):
                out["output"] = (pre + "\n" + str(out.get("output") or "")).strip()
            if out is None and self.no_submit and SUBMIT_MARKER in cmd:
                out = {"output": self.no_submit, "returncode": 1, "exception_info": ""}
            if out is not None:
                self._capture_management(cmd, out)
                if out.get("replaced"):          # the message list was rebuilt (handoff, compaction): no observation
                    self.free_turns += 1
                    return []
                outputs.append(out)
                continue
            self.token.check(self.name)
            left = int(max(1, self.ledger.seconds_left()))
            o = self.env.execute(action, timeout=min(left, getattr(self.env.config, "timeout", left)))
            o["output"] = truncate_output(str(o.get("output") or ""), self.config.max_output_chars)
            outputs.append(o)
            code_changed, mem_changed = self.tracker.observe()
            if code_changed or not mem_changed:  # a command that only rewrote the memory file is management
                counted = True
            self.after_command(code_changed)
        if counted:
            self.steps += 1
        else:
            self.free_turns += 1
        if outputs:
            last = outputs[-1]
            last["output"] = (str(last.get("output") or "").rstrip() + "\n" + self.readout_line()).lstrip("\n")
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    def after_command(self, code_changed: bool) -> None:
        pass

    def _capture_management(self, cmd: str, out: dict[str, Any]) -> None:
        """A management command never reaches the shell; it is still written to the session's command capture, so
        that the breach scanner sees every command of the trajectory (and reads these too)."""
        path = getattr(getattr(self.env, "config", None), "capture", None)
        if not path:
            return
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": getattr(self.env.config, "role", ""), "command": cmd,
                                 "output": str(out.get("output") or "")[:4000], "exit_code": 0, "seconds": 0.0,
                                 "management": True}) + "\n")

    # ------------------------------------------------------------------ compaction

    def compact(self, *, use_memory: bool, why: str, keep_recent: int | None = None) -> bool:
        """Replace the older turns by the memory file (when ``use_memory`` and it is fresh) or by a summary. One
        from the round's pool. False when nothing was compacted."""
        keep = self.config.keep_recent if keep_recent is None else int(keep_recent)
        starts = [i for i, m in enumerate(self.messages) if m.get("role") == "assistant" and i >= self.window.protect]
        if not starts or len(starts) <= keep:
            return False                                # nothing old enough: no compaction is spent
        cut = starts[-keep] if keep > 0 else len(self.messages)
        mem_ok = use_memory and not self.tracker.handoff_problems()
        if not mem_ok and self.summarizer is None:
            return False
        if not self.ledger.use_compression():
            return False
        head, tail = self.messages[:2], self.messages[cut:]
        if mem_ok:
            note = "[Context compacted: your earlier conversation was replaced by your memory file]\n\n" + MEM.read_memory(self.ws)
        else:
            text, p, c = self.summarizer(self.messages[2:cut])
            self.ledger.charge(p, c, quota_id=self.quota_id, what="compression")
            note = f"[Context compacted: summary of your earlier work in this session]\n{text}"
        self.messages = [*head, {"role": "user", "content": note}, *tail]
        self.window.reset_prefix(3)
        self.compactions += 1
        self.events.append({"event": "compaction", "call": self.n_calls, "why": why, "from_memory": bool(mem_ok)})
        self.ledger.note("compaction_done", session=self.name, why=why, from_memory=bool(mem_ok))
        return True

    def record(self) -> dict[str, Any]:
        return {"name": self.name, "calls": self.n_calls, "steps": self.steps, "free_turns": self.free_turns,
                "compactions": self.compactions, "context": self.window.stats(), "events": self.events}


class ExecutorAgent(ManagedAgent):
    def __init__(self, model, env, *, planner: Planner, stages: list[Any], arm: str,
                 spawn: Callable[[str, str, Any], dict[str, Any]], min_check: Callable[[], list[str]],
                 probe: Callable[[], dict[str, int]] | None = None, probe_every: int = 10, **kwargs):
        """``stages``: the units of the frozen plan (``brief()``). ``arm``: "pi0" | "evolved" | "single".
        ``spawn(kind, task, quota)`` runs a sub-agent and returns {"result": text, ...}. ``min_check()`` lists what
        the stage's minimum condition lacks. ``probe()`` measures {"missing": n, "tests_passed": n}."""
        super().__init__(model, env, **kwargs)
        self.planner, self.stages, self.arm = planner, stages, arm
        self.spawn, self.min_check, self.probe, self.probe_every = spawn, min_check, probe, probe_every
        self.declare_attempts: dict[int, int] = {}
        self.stage_records: list[dict[str, Any]] = []
        self.all_done = False
        self.signals = {"missing": None, "tests_passed": None, "missing_delta": 0, "tests_delta": 0}
        self.subagent_records: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self._last_probe_step = 0
        self.no_submit = "There is no submit command in this run: declare the current stage with `stage_done`."

    # ------------------------------------------------------------------ state and readout

    def tokens_left(self) -> int:
        return self.ledger.stage_left()

    def state(self) -> dict[str, Any]:
        return build_state(self.ledger, context_share=self.window.share(self.messages),
                           idle_steps=self.tracker.steps_since_code_change, missing_delta=self.signals["missing_delta"],
                           tests_delta=self.signals["tests_delta"], all_done=self.all_done)

    def readout_line(self) -> str:
        return readout(self.state(), self.ledger.current, self.ledger.n_stages)

    def measure(self) -> None:
        if self.probe is None:
            return
        t0 = time.time()
        got = self.probe()
        self.ledger.side_charge("harness", seconds=time.time() - t0, what="progress probe")
        for k, d in (("missing", "missing_delta"), ("tests_passed", "tests_delta")):
            if got.get(k) is not None:
                self.signals[d] = 0 if self.signals[k] is None else int(got[k] - self.signals[k])
                self.signals[k] = int(got[k])
        self.events.append({"event": "probe", "call": self.n_calls, **{k: self.signals[k] for k in ("missing", "tests_passed")}})

    def after_command(self, code_changed: bool) -> None:
        if self.probe_every and self.steps - self._last_probe_step >= self.probe_every:
            self._last_probe_step = self.steps
            self.measure()

    # ------------------------------------------------------------------ before every call: the planner

    def before_call(self) -> None:
        for act in self.planner.tick(self.state()):
            if act.action in ("framework_compaction", "force_compaction"):
                use_mem = bool(self.planner.skills.param("memory-compaction", "use_memory", False)) and act.action == "force_compaction"
                keep = self.planner.skills.param("memory-compaction", "keep_recent_turns", self.config.keep_recent)
                if not self.compact(use_memory=use_mem, why=act.action, keep_recent=keep):
                    continue
            self.add_messages({"role": "user", "content": act.text})
            self.events.append({"event": "planner", "call": self.n_calls, "skill": act.skill, "action": act.action})

    def out_of_tokens(self) -> None:
        """The stage's allocation is used up: the program closes the stage and the next one starts with the code
        as it is. After the last stage the round ends."""
        if self.all_done:
            raise _stop("RoundBudgetExhausted", "the budget is used up")
        k = self.ledger.current
        self._close_stage(declared=False, problems=[], why="allocation used up")
        if k + 1 >= len(self.stages):
            raise _stop("StageBudgetExhausted", "the last stage's allocation is used up")
        self.ledger.open_stage(k + 1)
        self.add_messages({"role": "user", "content":
                           f"[stage {k + 1}/{len(self.stages)} closed by the program: its token allocation is used up. "
                           f"The code stays as it is.]\n\nYou are now in stage {k + 2}/{len(self.stages)}:\n\n{self.stages[k + 1].brief()}"})

    def _close_stage(self, *, declared: bool, problems: list[str], why: str) -> None:
        self.measure()
        rec = self.ledger.close_stage(declared_done=declared, below_minimum=bool(problems), why=why)
        self.stage_records.append({**rec, "minimum_problems": problems, "missing": self.signals["missing"],
                                   "tests_passed": self.signals["tests_passed"], "calls_at_close": self.n_calls,
                                   "steps_at_close": self.steps})

    # ------------------------------------------------------------------ management commands

    def handle_management(self, verb: str, args: str) -> dict[str, Any] | None:
        text = {"stage_done": self._stage_done, "request_budget": self._request_budget, "subagent": self._subagent,
                "compact": self._compact}[verb](args)
        if text is None:
            return {"replaced": True}
        return {"output": text, "returncode": 0, "exception_info": ""}

    def _stage_done(self, args: str) -> str:
        k = self.ledger.current
        if self.all_done:
            return "stage_done: every stage is already declared done."
        n = self.declare_attempts.get(k, 0) + 1
        self.declare_attempts[k] = n
        t0 = time.time()
        problems = self.min_check()
        self.ledger.side_charge("harness", seconds=time.time() - t0, what="stage minimum check")
        if problems and n == 1:
            self.ledger.note("stage_minimum_failed", stage=k, problems=problems)
            return ("stage_done: not accepted yet. The stage's minimum condition is not met:\n"
                    + "\n".join(f"- {p}" for p in problems)
                    + "\nFix this and declare the stage done again (the next declaration is accepted either way).")
        self._close_stage(declared=True, problems=problems, why="declared")
        flag = " (recorded: below the minimum condition)" if problems else ""
        if k + 1 < len(self.stages):
            self.ledger.open_stage(k + 1)
            return (f"stage_done: stage {k + 1}/{len(self.stages)} closed{flag}.\n\n"
                    f"You are now in stage {k + 2}/{len(self.stages)}:\n\n{self.stages[k + 1].brief()}")
        if self.arm == "single":
            self.all_done = True
            self.ledger.stages[k].closed = False      # the single arm keeps working on the same budget
            return (f"stage_done: the last stage is declared done{flag}. Budget is left: keep improving the "
                    "repository until it is used up (compare the code with the design documents again, extend "
                    "your tests, fix what they show).")
        raise _stop("Submitted", f"all {len(self.stages)} stages declared done")

    def _request_budget(self, args: str) -> str:
        kv = parse_kv(args)
        cap = self.ledger.grant_cap()
        try:
            want = int(float(kv["tokens"])) if "tokens" in kv else int(float(kv["share"]) * cap) if "share" in kv else 0
        except ValueError:
            want = 0
        reason = kv.get("reason", "")
        self.requests.append({"stage": self.ledger.current, "requested": want, "reason": reason[:300], "call": self.n_calls})
        if want <= 0:
            return 'request_budget: say share=<0..1> (of what can be granted) or tokens=<N>, and reason="…"'
        got, text = self.planner.approve(want, reason, self.state())
        return text

    def compact_keep(self) -> int:
        return int(self.planner.skills.param("memory-compaction", "keep_recent_turns", self.config.keep_recent))

    def _subagent(self, args: str) -> str | None:
        if self.arm == "single":
            return "subagent: not available in this run."
        kv = parse_kv(args)
        kind, task = kv.get("type", ""), kv.get("task", "").strip()
        left = self.ledger.stage_left()
        tokens: int | None
        try:
            tokens = int(float(kv["tokens"])) if "tokens" in kv else int(float(kv["budget"]) * left) if "budget" in kv else None
        except ValueError:
            tokens = None
        if not task:
            return 'subagent: say what the sub-agent should do: task="…"'
        if kind == "worker":
            probs = self.tracker.handoff_problems()
            if probs:
                self.ledger.note("handoff_refused", problems=probs)
                return ("subagent: handoff refused. Update the memory file first:\n" + "\n".join(f"- {p}" for p in probs))
        margin = self.window.count(self.messages) + self.config.reserve_tokens
        try:
            quota = self.ledger.reserve(kind, tokens, margin=margin)
        except Refused as e:
            hint = ""
            if tokens is None and kind in ("review", "repair", "worker"):
                hint = f" Suggested: budget={self.planner.quota_share(kind, self.state()):.2f} (the share of what this stage has left)."
            return f"subagent: refused. {e}.{hint}"
        prev = self.token.take(quota.quota_id)
        try:
            rec = self.spawn(kind, task, quota)
        finally:
            self.token.give_back(prev)
            back = self.ledger.release(quota.quota_id)
        self.tracker.observe()                         # the sub-agent's changes are the code's state now
        rec = {**rec, "kind": kind, "quota": quota.tokens, "used": quota.used, "returned": back, "stage": quota.stage,
               "cut": tokens is not None and quota.tokens < tokens}
        self.subagent_records.append(rec)
        result = str(rec.get("result") or "(the sub-agent returned nothing)")[:6000]
        head = (f"subagent {kind}: finished ({rec.get('exit_status')}); used {quota.used} of {quota.tokens} tokens"
                + (" (the quota was cut to what could be given)" if rec["cut"] else "") + ".")
        if kind != "worker":
            return f"{head}\n\n{result}"
        if not rec.get("wrote_memory"):
            self.tracker.append_worker_result(result)
        self.tracker.fresh = True
        self.messages = [*self.messages[:2],
                         {"role": "user", "content": f"[Handoff finished. {head} Your context was replaced by the memory "
                                                     f"file; continue from it.]\n\n{MEM.read_memory(self.ws)}\n\n"
                                                     f"{self.readout_line()}"}]
        self.window.reset_prefix(3)
        self.events.append({"event": "handoff", "call": self.n_calls})
        return None

    def record(self) -> dict[str, Any]:
        return {**super().record(), "stages": self.stage_records, "subagents": self.subagent_records,
                "requests": self.requests, "signals": self.signals, "declare_attempts": self.declare_attempts}
