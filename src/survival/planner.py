"""The planner: skill documents turned into actions (refactor §4).

``state`` builds what the planner sees (§4.1: ratios, counts and flags, nothing of the task). ``tick`` evaluates
every skill's triggers on it before each executor call; a trigger fires when its condition turns true and is armed
again when it turns false. Requests that need a judgment (how much of a top-up, how large a quota) are one model
call with the skill's body, the state and the cap as input, cut to the cap; the planner's own tokens are booked on
the management side account, never on the round's budget (§4.4).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from survival.ledger import RoundLedger
from survival.skills import STATE, SkillSet, evaluate

Complete = Callable[[list[dict]], tuple[str, int, int]]

JUDGE_SYSTEM = """You are the resource planner of a coding agent. You see only resource figures (shares and counts),
never the task. Follow the skill below. Answer with one JSON object and nothing else:
{"share": <a number between 0 and 1: the part of the cap to give>, "reason": "<one sentence>"}"""


@dataclass
class Action:
    skill: str
    action: str
    text: str            # what the executor is told (may be empty for a silent action)


def build_state(ledger: RoundLedger, *, context_share: float, idle_steps: int, missing_delta: int, tests_delta: int,
                all_done: bool) -> dict[str, Any]:
    lim = ledger.limits
    s = ledger.stage()
    st = {
        "stage_tokens": round(min(1.0, (s.used + s.reserved) / s.allocation), 4) if s.allocation else 1.0,
        "round_tokens": round(min(1.0, ledger.used / lim.tokens), 4) if lim.tokens else 1.0,
        "context": round(min(1.0, context_share), 4),
        "compressions": ledger.compressions, "compressions_limit": lim.compressions,
        "reviews": ledger.calls["review"], "reviews_limit": lim.reviews,
        "repairs": ledger.calls["repair"], "repairs_limit": lim.repairs,
        "workers": ledger.calls["worker"], "workers_limit": lim.workers,
        "subagents": ledger.subagents_used(), "subagents_limit": lim.subagents,
        "idle_steps": int(min(idle_steps, 100)),
        "missing_delta": int(max(-100, min(100, missing_delta))),
        "tests_delta": int(max(-100, min(100, tests_delta))),
        "stage_progress": round((s.index + 1) / ledger.n_stages, 4),
        "stage_done": bool(all_done),
    }
    assert set(st) == set(STATE)
    return st


def readout(state: dict[str, Any], stage_index: int, n_stages: int, *, stage_tokens: float | None = None) -> str:
    """The one line appended to every tool result (§3.1): the same format for every arm and every session."""
    pct = lambda x: f"{int(round(100 * x))}%"  # noqa: E731
    st = state["stage_tokens"] if stage_tokens is None else stage_tokens
    return (f"[resources: stage {stage_index + 1}/{n_stages} | stage tokens {pct(st)} used | "
            f"round tokens {pct(state['round_tokens'])} used | context {pct(state['context'])} | "
            f"compressions {state['compressions']}/{state['compressions_limit']} | "
            f"repairs {state['repairs']}/{state['repairs_limit']} | "
            f"subagents {state['subagents']}/{state['subagents_limit']}]")


READOUT_RE = re.compile(r"^\[resources: stage \d+/\d+ \| stage tokens \d+% used \| round tokens \d+% used \| context \d+% \| "
                        r"compressions \d+/\d+ \| repairs \d+/\d+ \| subagents \d+/\d+\]$")


class Planner:
    def __init__(self, skills: SkillSet, ledger: RoundLedger, *, complete: Complete | None = None):
        self.skills, self.ledger, self.complete = skills, ledger, complete
        self._armed: dict[tuple[str, int], bool] = {}
        self._last_repeat: dict[tuple[str, int], int] = {}
        self.decisions: list[dict[str, Any]] = []
        self.fired: list[dict[str, Any]] = []
        self.ticks = 0

    # ------------------------------------------------------------------ triggers

    def tick(self, state: dict[str, Any]) -> list[Action]:
        self.ticks += 1
        out: list[Action] = []
        for name, skill in self.skills.skills.items():
            for i, t in enumerate(skill.triggers):
                key = (name, i)
                on = evaluate(t.tree, state)
                was = self._armed.get(key, False)
                self._armed[key] = on
                repeat = bool(skill.params.get("repeat")) and on and self.ticks - self._last_repeat.get(key, -10**9) >= 5
                if on and (not was or repeat):
                    self._last_repeat[key] = self.ticks
                    out.append(Action(skill=name, action=t.action, text=self.message(name, t.action, state)))
                    self.fired.append({"tick": self.ticks, "skill": name, "action": t.action, "when": t.when,
                                       "state": {k: state[k] for k in ("stage_tokens", "round_tokens", "context")},
                                       "stage": self.ledger.current})
        return out

    def message(self, name: str, action: str, state: dict[str, Any]) -> str:
        sk = self.skills[name]
        text = f"[planner: {name}] {sk.section('What to do')}\n\nHow to tell it worked: {sk.section('Checking it worked')}"
        if action in ("suggest_handoff", "suggest_review", "suggest_repair"):
            kind = {"suggest_handoff": "worker", "suggest_review": "review", "suggest_repair": "repair"}[action]
            share = self.quota_share(kind, state)
            text += (f"\n\nSuggested call: `subagent type={kind} budget={share:.2f} task=\"...\"` "
                     "(budget is the share of what this stage has left).")
        return text

    # ------------------------------------------------------------------ judgments (§4.4)

    def _judge(self, skill: str, what: str, state: dict[str, Any], fallback: float) -> tuple[float, str, str]:
        """(share of the cap, reason, decided_by). One model call; any failure falls back to ``fallback``."""
        if self.complete is None:
            return fallback, "no planner model in this run", "fixed"
        user = json.dumps({"request": what, "state": state}, indent=1)
        try:
            text, p, c = self.complete([{"role": "system", "content": JUDGE_SYSTEM + "\n\n" + self.skills[skill].text},
                                        {"role": "user", "content": user}])
            self.ledger.side_charge("management", tokens=p, what=f"planner {skill}")
            m = re.search(r"\{.*\}", text, re.S)
            d = json.loads(m.group(0)) if m else {}
            share = float(d.get("share"))
            return max(0.0, min(1.0, share)), str(d.get("reason") or "")[:300], "planner"
        except Exception as e:  # noqa: BLE001 - a planner failure never stops a round
            return fallback, f"planner call failed ({type(e).__name__})", "fixed"

    def quota_share(self, kind: str, state: dict[str, Any]) -> float:
        """The share of the stage's remaining tokens suggested for a sub-agent of ``kind``."""
        if kind == "worker":
            sk, share = "subagent-handoff", float(self.skills.param("subagent-handoff", "quota_share", 0.2))
        else:
            sk = "review-repair-dispatch"
            d = float(self.skills.param(sk, "default_quota_share", 0.2))
            share = float(self.skills.param(sk, f"{kind}_quota_share", d))
        if self.skills.param(sk, "mode", "fixed") == "judge":
            share, _, _ = self._judge(sk, f"quota for a {kind} sub-agent, as a share of the stage's remaining tokens", state, share)
        return share

    def approve(self, requested: int, reason: str, state: dict[str, Any]) -> tuple[int, str]:
        """A top-up request: (granted tokens, the answer the executor reads)."""
        cap = self.ledger.grant_cap()
        mode = self.skills.param("budget-approval", "mode", "approve_within_cap")
        max_share = float(self.skills.param("budget-approval", "max_grant_share", 1.0))
        want_share = min(1.0, requested / cap) if cap > 0 else 0.0
        if mode == "judge":
            share, why, by = self._judge("budget-approval",
                                         f"a top-up of {want_share:.2f} of the cap; the executor's reason: {reason[:400]}",
                                         state, min(want_share, max_share))
            share = min(share, max_share)
        else:
            share, why, by = min(want_share, max_share), "approved within the cap", "fixed"
        got = self.ledger.grant(min(int(share * cap), int(requested)), requested=requested, reason=reason, decided_by=by)
        self.decisions.append({"kind": "topup", "stage": self.ledger.current, "requested": requested, "cap": cap,
                               "share_of_cap": round(share, 4), "granted": got, "decided_by": by, "why": why})
        if got <= 0:
            return 0, f"request_budget: refused ({why if cap > 0 else 'nothing can be granted: the later stages keep their floors'})."
        return got, (f"request_budget: granted {got} tokens" + (f" of the {requested} asked" if got < requested else "")
                     + f". {why}".rstrip())
