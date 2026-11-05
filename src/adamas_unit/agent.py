"""A mini-swe-agent agent that lives inside the budget ledger (docs/USING_ADAMAS.md §3.3, DESIGN §5–§6).

``BudgetedAgent`` overrides one point of mini-swe-agent's loop, ``query``:

- before a model call: stop when the round budget is exhausted, when the lease has fewer tokens left than the safety
  margin (§5.4), or when its time is up; when the context is over the window, compress once (a pool shared by the
  round) or stop when no compression is left;
- after it: book the call's prompt tokens on the lease (from the reply's ``usage``; a format-error reply is booked
  too), and every ``probe_every`` calls let the driver measure progress (§8.4).

Two more things are intercepted in ``execute_actions``:

- ``request_budget tokens=N|seconds=N reason=...``: a top-up request, handed to the planner callback; its answer is
  returned as the command's output (the command never reaches the shell);
- the per-command timeout is lowered to the time the lease has left.

Every stop is an ``exit`` message with an ``exit_status`` the unit runner reads (``RoundBudgetExhausted``,
``TokensExhausted``, ``TimeExceeded``, ``ContextExhausted``, ``Reclaimed``, ``Submitted``, ...).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.exceptions import FormatError, LimitsExceeded

from adamas_unit.ledger import Ledger, Lease

REQUEST = re.compile(r"^\s*request_budget\b(.*)$", re.S)


class BudgetedAgentConfig(AgentConfig):
    margin_tokens: int = 60_000          # one call's worst case: stop when less than this is left (§5.4)
    context_window: int = 200_000        # compress when the last prompt exceeded this many tokens
    keep_recent: int = 6                 # assistant turns kept verbatim by a compression
    probe_every: int = 0                 # progress measurement every N calls (0: off)
    step_limit: int = 0
    cost_limit: float = 0.0              # dollars are not used; tokens are (the channel's prices are unknown)


def _stop(status: str, text: str = "") -> LimitsExceeded:
    return LimitsExceeded({"role": "exit", "content": text or status, "extra": {"exit_status": status, "submission": ""}})


def usage_of(message: dict) -> tuple[int, int]:
    """(prompt, completion) tokens of one model reply (cached prompt tokens included in prompt)."""
    resp = (message.get("extra") or {}).get("response") or {}
    u = resp.get("usage") if isinstance(resp, dict) else None
    u = u or {}
    return int(u.get("prompt_tokens") or u.get("input_tokens") or 0), int(u.get("completion_tokens") or u.get("output_tokens") or 0)


class BudgetedAgent(DefaultAgent):
    def __init__(self, model, env, *, ledger: Ledger, lease: Lease,
                 planner: Callable[[dict[str, Any]], str] | None = None,
                 summarizer: Callable[[list[dict]], tuple[str, int, int]] | None = None,
                 progress: Callable[["BudgetedAgent"], str] | None = None, **kwargs):
        super().__init__(model, env, config_class=BudgetedAgentConfig, **kwargs)
        self.ledger, self.lease = ledger, lease
        self.planner, self.summarizer, self.progress = planner, summarizer, progress
        self.last_prompt_tokens = 0
        self.compressions = 0
        self.requests: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ the one override of the loop

    def query(self) -> dict:
        lid = self.lease.lease_id
        if self.ledger.exhausted():
            raise _stop("RoundBudgetExhausted", f"the round's {self.ledger.exhausted()} budget is used up")
        if self.lease.closed:
            raise _stop("Reclaimed", "the planner took this session's budget back")
        if self.lease.tokens_left() < self.config.margin_tokens:
            raise _stop("TokensExhausted", f"{self.lease.tokens_left()} tokens left, below the margin")
        if self.ledger.seconds_left(lid) <= 0:
            raise _stop("TimeExceeded")
        if self.last_prompt_tokens > self.config.context_window:
            self._compress()
        self.n_calls += 1
        try:
            message = self.model.query(self.messages)
        except FormatError as e:
            p, c = usage_of(e.messages[0])
            self.ledger.charge(lid, p, c, what="format_error")
            self.last_prompt_tokens = p
            raise
        p, c = usage_of(message)
        self.ledger.charge(lid, p, c)
        self.last_prompt_tokens = p
        self.add_messages(message)
        if self.progress and self.config.probe_every and self.n_calls % self.config.probe_every == 0:
            verdict = self.progress(self)
            if verdict == "reclaim":
                self.ledger.reclaim(lid, reason="no-progress loop")
        return message

    def execute_actions(self, message: dict) -> list[dict]:
        outputs = []
        for action in message.get("extra", {}).get("actions", []):
            m = REQUEST.match(str(action.get("command") or ""))
            if m:
                outputs.append({"output": self._request(m.group(1)), "returncode": 0, "exception_info": ""})
                continue
            left = int(max(1, self.ledger.seconds_left(self.lease.lease_id)))
            outputs.append(self.env.execute(action, timeout=min(left, getattr(self.env.config, "timeout", left))))
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    # ------------------------------------------------------------------ top-up requests (§5.3)

    def _request(self, args: str) -> str:
        kv = dict(re.findall(r"(\w+)=(\"[^\"]*\"|\S+)", args))
        kind = "tokens" if "tokens" in kv else "seconds" if "seconds" in kv else ""
        try:
            amount = float(kv.get(kind, "0"))
        except ValueError:
            amount = 0.0
        reason = kv.get("reason", "").strip('"') or args.strip()
        req = {"lease": self.lease.lease_id, "unit": self.lease.unit, "role": self.lease.role, "kind": kind,
               "amount": amount, "reason": reason, "calls": self.n_calls, "time": time.time()}
        self.requests.append(req)
        if not kind or amount <= 0:
            return "request_budget: say tokens=<N> or seconds=<N> and reason=\"...\""
        if self.planner is None:
            return "request_budget: refused (no budget planner in this run)"
        return self.planner(req)

    # ------------------------------------------------------------------ compression (§6)

    def _compress(self) -> None:
        # cut before an assistant turn so that no tool call loses its result
        starts = [i for i, m in enumerate(self.messages) if m.get("role") == "assistant" and i >= 2]
        if len(starts) <= self.config.keep_recent:
            return                                      # nothing old enough to summarise: no compression is spent
        if self.summarizer is None or not self.ledger.use_compression(self.lease.unit):
            raise _stop("ContextExhausted", "the context is over the window and no compression is left")
        cut = starts[-self.config.keep_recent]
        text, p, c = self.summarizer(self.messages[2:cut])
        self.ledger.charge(self.lease.lease_id, p, c, what="compression")
        self.messages = [*self.messages[:2],
                         {"role": "user", "content": f"[Summary of your earlier work in this session]\n{text}"},
                         *self.messages[cut:]]
        self.compressions += 1
        self.last_prompt_tokens = 0
