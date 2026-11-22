"""The v0.3 executor on a ContextBench stream (docs/CONTEXTBENCH.md §5).

``StreamAgent`` is a ``ManagedAgent`` (ledger, resource readout, context window with roll-back, ``compact``) whose
task arrives one operation at a time: after a command, ContextBench's ``TurnSource`` decides what follows it (the
next operation, a refusal of an early submit, an explanation of a marker that was not standalone) and each of those
texts is appended as a user message after the tool result. An operation reaches the context by no other route.

What differs from the repository executor, and why:

- the context is counted with ContextBench's own uncalibrated o200k_base count, against budget - reserve (user
  decision 2026-11-22: triggers and the overflow test use the benchmark's ruler; booking uses the server's tokens);
- only the first tool call of a reply runs (as ContextBench's own agent: one command, then what the stream releases);
- a roll-back never removes a delivered operation (it could not be delivered again): the operation is put back, and
  if the context is still over the window it is compacted (arms with skills) or its oldest messages are dropped
  (arm ``none``);
- snapshots for the graders, written by ContextBench's ``record_snapshot``: the context of every model call right
  before it is sent (``sent``), the context before every rewrite (``turn``), and the last context (``final``);
- arms: ``pi0`` / ``evolved`` run the planner's triggers before every call; ``none`` has no planner, no ``compact``
  and no reminder: when the next request would not fit, the oldest messages are dropped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from minisweagent.exceptions import Submitted

from survival import cb as CB
from survival import memory as MEM
from survival.agents import ManagedAgent, _stop
from survival.context import ContextWindow
from survival.planner import Planner, build_state, readout

ARMS = ("pi0", "evolved", "none")
MEMORY_TEMPLATE = "# Memory\n\n(nothing yet)\n"
STREAM = "cb_stream"                     # extra-key of a user message the stream delivered


class StreamWindow(ContextWindow):
    """The window in ContextBench's ruler. The server's prompt tokens are recorded, never applied."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.peak_o200k = 0
        self.served: list[float] = []

    def count(self, messages: list[dict]) -> int:
        return CB.count(messages)

    def calibrate(self, sent: list[dict], prompt_tokens: int) -> None:
        raw = CB.count(sent)
        self.peak_o200k = max(self.peak_o200k, raw)
        self.peak = max(self.peak, int(prompt_tokens))
        if raw > 0 and prompt_tokens > 0:
            self.served.append(prompt_tokens / raw)

    def stats(self) -> dict[str, Any]:
        s = super().stats()
        s["peak_o200k"] = self.peak_o200k
        s["served_per_o200k"] = round(sum(self.served) / len(self.served), 3) if self.served else None
        return s


class StreamMemory(MEM.MemoryTracker):
    """The memory file of a stream run has no fixed headings: it is usable for a compaction when the agent has
    written it since the last compaction that used it."""

    def observe(self) -> tuple[bool, bool]:
        code_changed, mem_changed = super().observe()
        if mem_changed:
            self.fresh = MEM.read_memory(self.ws).strip() not in ("", MEMORY_TEMPLATE.strip())
        elif code_changed:
            self.fresh = self._fresh_before      # files written to disk do not make the memory file stale here
        self._fresh_before = self.fresh
        return code_changed, mem_changed

    _fresh_before = False

    def handoff_problems(self) -> list[str]:
        return [] if self.fresh else ["the memory file was not updated since the last compaction"]


class DiskWatch:
    """Whether task content was written to disk (the compliance figure of a run; user decision 2026-11-22).

    needle_retention and sudoku_sketchpad tell the agent not to write to disk; kv_store and log_triage allow it.
    The figure is recorded for every run all the same. After each command the files that changed in the working
    directory and in the session's private directory are read; a file holds task content when it has a line of a
    delivered operation (or of the instruction's starting board), one of the operations' ``#xxxxxxxx`` tags, or a
    run of board tuples. The memory file is counted separately from other files."""

    TAG = re.compile(r"#[0-9a-f]{8}\b")
    TUPLES = re.compile(r"(?:\(\d+,\d+,[^)\s]\)\s*){8,}")
    MAX_BYTES = 16_000_000

    def __init__(self, roots: list[Path], memory: Path, skip: set[str] | None = None):
        self.roots, self.memory, self.skip = [Path(r) for r in roots], Path(memory), skip or set()
        self.lines: set[str] = set()
        self.tags: set[str] = set()
        self.sig: dict[str, tuple[int, int]] = {}
        self.memory_file = self.other_files = False
        self.first_call: int | None = None
        self.checks = self.hits = 0

    def add(self, text: str) -> None:
        for ln in text.splitlines():
            s = ln.strip()
            if len(s) >= 16:
                self.lines.add(s)
        self.tags.update(self.TAG.findall(text))

    def _has_content(self, text: str) -> bool:
        if self.tags.intersection(self.TAG.findall(text)) or self.TUPLES.search(text):
            return True
        return any(ln.strip() in self.lines for ln in text.splitlines())

    def check(self, call: int) -> None:
        self.checks += 1
        for root in self.roots:
            for p in root.rglob("*"):
                try:
                    if p.is_symlink() or not p.is_file() or p.name in self.skip:
                        continue
                    st = p.stat()
                    sig = (st.st_mtime_ns, st.st_size)
                    if self.sig.get(str(p)) == sig:
                        continue
                    self.sig[str(p)] = sig
                    if st.st_size > self.MAX_BYTES:
                        continue
                    if not self._has_content(p.read_text(encoding="utf-8", errors="replace")):
                        continue
                except OSError:
                    continue
                self.hits += 1
                if p == self.memory:
                    self.memory_file = True
                else:
                    self.other_files = True
                if self.first_call is None:
                    self.first_call = call

    def record(self) -> dict[str, Any]:
        return {"task_content_on_disk": self.memory_file or self.other_files, "in_memory_file": self.memory_file,
                "in_other_files": self.other_files, "first_call": self.first_call}


class StreamEnvironmentMixin:
    """Records the last command and its exit code; a submit marker is an ordinary command here (the stream decides
    whether the run may end)."""

    last: tuple[str, int] | None = None

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        try:
            out = super().execute(action, cwd, timeout=timeout)
        except Submitted as e:
            out = {"output": CB.texts()["submit"] + "\n" + str(e.messages[0].get("content") or ""), "returncode": 0, "exception_info": ""}
        self.last = (str(action.get("command") or ""), int(out.get("returncode") or 0))
        return out


class StreamAgent(ManagedAgent):
    def __init__(self, model, env, *, source, logs_dir: Path, arm: str, planner: Planner | None, **kwargs):
        if arm not in ARMS:
            raise ValueError(arm)
        super().__init__(model, env, **kwargs)
        self.source, self.logs_dir, self.arm, self.planner = source, Path(logs_dir), arm, planner
        if (planner is None) != (arm == "none"):
            raise ValueError("the arm `none` has no planner; every other arm has one")
        self.window = StreamWindow(self.config.context_window, max_retries=self.config.max_rollbacks)
        self.tracker = StreamMemory(self.ws)
        self.snap_step = 0
        self.truncations = 0
        self.dropped_by_truncation = 0
        self.trace: list[dict[str, Any]] = []
        self.format_hint = CB.texts()["no_tool_call"]
        self._started = False
        self.disk = DiskWatch([self.ws, Path(getattr(env, "private", self.ws))], MEM.memory_path(self.ws), skip={"policy.json"})
        self.cached_tokens = 0

    # ------------------------------------------------------------------ state, readout, budget

    def tokens_left(self) -> int:
        return self.ledger.stage_left()

    def out_of_tokens(self) -> None:
        raise _stop("TokensExhausted", "the run's token cap is used up")

    def state(self) -> dict[str, Any]:
        return build_state(self.ledger, context_share=self.window.share(self.messages),
                           idle_steps=self.tracker.steps_since_code_change, missing_delta=0, tests_delta=0, all_done=False)

    def readout_line(self) -> str:
        return readout(self.state(), self.ledger.current, self.ledger.n_stages)

    def compact_keep(self) -> int:
        if self.planner is None:
            return self.config.keep_recent
        return int(self.planner.skills.param("memory-compaction", "keep_recent_turns", self.config.keep_recent))

    # ------------------------------------------------------------------ snapshots

    def _snapshot(self, messages: list[dict], kind: str) -> None:
        b, r = CB.budget()
        CB.snapshot(self.logs_dir, self.snap_step, messages,
                    {"kind": kind, "n_ops_delivered": self.source.n_delivered, "n_ops_total": self.source.n_total,
                     "budget": b, "reserve": r})
        self.snap_step += 1

    def on_send(self, sent: list[dict]) -> None:
        self._snapshot(sent, "sent")
        self.trace.append({"call": self.n_calls, "ops": self.source.n_delivered, "context": CB.count(sent)})

    def before_rewrite(self, why: str) -> None:
        self._snapshot(self.messages, "turn")

    def final_snapshot(self) -> None:
        self._snapshot(self.messages, "final")

    # ------------------------------------------------------------------ before every call

    def query(self) -> dict:
        if not self._started:                           # operation 0 follows the instruction
            self._started = True
            first = self.source.first_turn(turn=0)
            if first is None:
                raise _stop("StreamEnded", "the stream is empty")
            self._deliver([first])
        message = super().query()
        usage = ((message.get("extra") or {}).get("response") or {}).get("usage") or {}
        self.cached_tokens += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        return message

    def before_call(self) -> None:
        if self.planner is None:
            self._truncate()
            return
        for act in self.planner.tick(self.state()):
            if act.action in ("framework_compaction", "force_compaction"):
                use_mem = bool(self.planner.skills.param("memory-compaction", "use_memory", False)) and act.action == "force_compaction"
                if not self.compact(use_memory=use_mem, why=act.action, keep_recent=self.compact_keep()):
                    continue
            self.add_messages({"role": "user", "content": act.text})
            self.events.append({"event": "planner", "call": self.n_calls, "skill": act.skill, "action": act.action,
                                "ops": self.source.n_delivered})

    def _truncate(self) -> None:
        """Arm ``none``: when the next request would not fit, the oldest messages go (never the system prompt, the
        instruction, or the newest message); a tool result never stays without its call."""
        if not self.window.over(self.messages):
            return
        p = self.window.protect
        before = len(self.messages)
        self.before_rewrite("truncation")
        while self.window.over(self.messages) and len(self.messages) > p + 1:
            self.messages.pop(p)
            while len(self.messages) > p + 1 and self.messages[p].get("role") == "tool":
                self.messages.pop(p)
        self.truncations += 1
        self.dropped_by_truncation += before - len(self.messages)
        self.events.append({"event": "truncation", "call": self.n_calls, "dropped": before - len(self.messages),
                            "ops": self.source.n_delivered})

    # ------------------------------------------------------------------ overflow

    def _overflow(self) -> None:
        if self.window.retries >= self.config.rollback_limit:
            self.events.append({"event": "rollback_limit", "call": self.n_calls, "rollbacks": self.window.retries})
            self.ledger.note("rollback_limit", session=self.name, rollbacks=self.window.retries)
            raise _stop("RollbackLimit", f"the context overflowed {self.window.retries} times in this session")
        if self.planner is not None and not self.window.can_retry():
            self._force_compaction("forced after the last retry")
            if not self.window.over(self.messages):
                return
        before = self.window.count(self.messages)
        self.before_rewrite("rollback")
        dropped, after = self.window.roll_back(self.messages)
        self.window.pin_note(self.messages, self._rollback_text(len(dropped)))
        # what the program put there stays: a planner message fires once per crossing, and a delivered operation
        # cannot be delivered again
        kept = [m for m in dropped if m.get("role") == "user" and (
            str(m.get("content") or "").startswith("[planner:") or (m.get("extra") or {}).get(STREAM))]
        self.messages.extend(kept)
        self.ledger.note_rollback(session=self.name, dropped=len(dropped) - len(kept), before=before,
                                  after=self.window.count(self.messages), retry=self.window.retries)
        self.events.append({"event": "rollback", "call": self.n_calls, "dropped": len(dropped) - len(kept),
                            "ops": self.source.n_delivered})
        if self.window.over(self.messages):
            if self.planner is None:
                self._truncate()
            else:
                self._force_compaction("forced: over the window after the roll-back")
        if self.window.over(self.messages):
            raise _stop("ContextExhausted", "the context is over the window with nothing left to undo or compact")

    def _force_compaction(self, why: str) -> None:
        for k in (self.compact_keep(), 2, 1):
            if not self.window.over(self.messages):
                return
            self.compact(use_memory=True, why=why, keep_recent=k)

    def _rollback_text(self, n_dropped: int) -> str:
        if self.planner is not None:
            return self.window.rollback_text(n_dropped)
        w = self.window
        t = (f"[context window] Your last {n_dropped} message(s) made the context larger than the window, so they were "
             f"undone and are no longer in your context (retry {w.retries}/{w.max_retries}; the tokens they cost are "
             "spent). Go on with commands that print less.")
        if w.overflow_cmds:
            t += "\nCommands whose output overflowed the window (do not run them again unchanged):\n" + "\n".join(
                f"  - {c}" for c in w.overflow_cmds)
        return t

    def compact(self, *, use_memory: bool, why: str, keep_recent: int | None = None) -> bool:
        n = len(self.events)
        done = super().compact(use_memory=use_memory, why=why, keep_recent=keep_recent)
        if done:
            ev = self.events[n]
            ev["ops"] = self.source.n_delivered
            if ev.get("from_memory"):
                self.tracker.fresh = self.tracker._fresh_before = False
        return done

    def handle_management(self, verb: str, args: str) -> dict[str, Any] | None:
        if verb == "compact" and self.planner is None:
            return {"output": "compact: not available in this run.", "returncode": 1, "exception_info": ""}
        return super().handle_management(verb, args)

    # ------------------------------------------------------------------ one command, then the stream

    def _deliver(self, texts: list[str]) -> None:
        for t in texts:
            self.disk.add(t)
            self.add_messages({"role": "user", "content": t, "extra": {STREAM: True}})

    def execute_actions(self, message: dict) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        if len(actions) > 1:                            # one command per reply; the rest is dropped from the message
            message["extra"]["actions"] = actions[:1]
            if message.get("tool_calls"):
                message["tool_calls"] = message["tool_calls"][:1]
            self.events.append({"event": "extra_tool_calls_dropped", "call": self.n_calls, "n": len(actions) - 1})
        self.env.last = None
        added = super().execute_actions(message)
        if self.env.last is not None:
            cmd, code = self.env.last
            self.disk.check(self.n_calls)               # before the next operation is known: what this command wrote
            self._deliver(self.source.pending(cmd, code, turn=self.n_calls))
        if self.source.done:
            raise _stop("StreamEnded", f"the stream ended ({self.source.terminated_by})")
        return added

    def record(self) -> dict[str, Any]:
        return {**super().record(), "truncations": self.truncations, "dropped_by_truncation": self.dropped_by_truncation,
                "snapshots": self.snap_step, "trace": self.trace, "disk": self.disk.record(), "cached_tokens": self.cached_tokens}
