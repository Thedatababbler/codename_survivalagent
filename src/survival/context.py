"""Context size, the window, and the roll-back when a turn would overflow it (refactor §3.2).

The size of the next request is counted locally before it is sent (tiktoken; scaled by a ratio learned from the
server's reported prompt tokens, so that the count follows the served tokenizer). Counting before sending is what
lets a turn be undone: when the request would exceed the window, the newest turns are dropped from the message list
until ``retry_margin`` tokens are free, the session is told to tidy its memory and compact, and a pinned note names
the commands whose output overflowed (so the same command is not simply run again). The number of such retries per
session is capped. Tokens already spent are not returned: the ledger was charged when each call came back.

The mechanism follows the context-budget controller of the Context Language Models reference implementation
(count before send, roll back the newest turns, never split a call from its result, a durable note of what
overflowed); the code is written for this project (docs/V03_MAPPING.md).
"""

from __future__ import annotations

import json
from typing import Any

_ENC = None


def _encoder():
    global _ENC
    if _ENC is None:
        try:
            import tiktoken

            _ENC = tiktoken.get_encoding("o200k_base")
        except Exception:  # noqa: BLE001 - no encoder (offline): fall back to a character estimate
            _ENC = False
    return _ENC


def _text_of(message: dict) -> str:
    c = message.get("content")
    if isinstance(c, list):
        c = " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in c)
    parts = [str(c or "")]
    for tc in message.get("tool_calls") or []:
        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
        parts.append(str(fn.get("arguments", "")))
    for a in (message.get("extra") or {}).get("actions") or []:
        if not message.get("tool_calls"):
            parts.append(str(a.get("command", "")))
    return "\n".join(parts)


def count_text(text: str) -> int:
    enc = _encoder()
    if not enc:
        return len(text) // 4 + 1
    return len(enc.encode(text, disallowed_special=()))


def count_messages(messages: list[dict]) -> int:
    return sum(count_text(_text_of(m)) + 4 for m in messages if m.get("role") != "exit")


class ContextWindow:
    """The window of one session: counting, calibration, roll-back bookkeeping."""

    def __init__(self, window: int, *, max_retries: int = 6, retry_margin: int = 4000, protect: int = 2):
        self.window, self.max_retries, self.retry_margin = int(window), int(max_retries), int(retry_margin)
        self.protect = protect                 # leading messages a roll-back keeps (system, task, pinned notes)
        self.ratio = 1.0
        self.retries = 0
        self.dropped_turns = 0
        self.peak = 0
        self.overflow_cmds: list[str] = []
        self._ledger_index: int | None = None

    def count(self, messages: list[dict]) -> int:
        return int(count_messages(messages) * self.ratio)

    def share(self, messages: list[dict]) -> float:
        return self.count(messages) / self.window if self.window else 0.0

    def calibrate(self, sent: list[dict], prompt_tokens: int) -> None:
        raw = count_messages(sent)
        if raw > 0 and prompt_tokens > 0:
            r = prompt_tokens / raw
            if 0.5 <= r <= 3.0:                # anything wilder is a counting accident, not a tokenizer
                self.ratio = r
        self.peak = max(self.peak, int(prompt_tokens))

    def over(self, messages: list[dict]) -> bool:
        return self.count(messages) > self.window

    def can_retry(self) -> bool:
        return self.retries < self.max_retries

    def roll_back(self, messages: list[dict]) -> tuple[list[dict], int]:
        """Drop the newest turns (in place) until ``retry_margin`` is free; never leave an assistant turn without
        its result. Returns (dropped messages, tokens afterwards)."""
        depth = max(1, self.retries - 2)               # after three roll-backs in a row, dig deeper each time
        target = max(self.window - self.retry_margin * depth, self.window // 4)
        dropped: list[dict] = []
        while len(messages) > self.protect and self.count(messages) > target:
            dropped.append(messages.pop())
            while len(messages) > self.protect and messages[-1].get("role") == "assistant":
                dropped.append(messages.pop())
        while len(messages) > self.protect:            # never keep a call whose results are no longer all there
            i = max((j for j, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
            if i < self.protect:
                break
            calls = len(messages[i].get("tool_calls") or [])
            if calls and sum(1 for m in messages[i + 1:] if m.get("role") == "tool") < calls:
                while len(messages) > i:
                    dropped.append(messages.pop())
            else:
                break
        self.retries += 1
        self.dropped_turns += len(dropped)
        for m in dropped:
            for a in (m.get("extra") or {}).get("actions") or []:
                cmd = " ".join(str(a.get("command", "")).split())[:70]
                if cmd and cmd not in self.overflow_cmds:
                    self.overflow_cmds.append(cmd)
        del self.overflow_cmds[:-5]
        return list(reversed(dropped)), self.count(messages)

    def pin_note(self, messages: list[dict], text: str) -> None:
        """Keep one note about the roll-backs right after the protected prefix (updated in place afterwards)."""
        note = {"role": "user", "content": text}
        if self._ledger_index is None or self._ledger_index >= len(messages):
            messages.insert(self.protect, note)
            self._ledger_index = self.protect
            self.protect += 1
        else:
            messages[self._ledger_index] = note

    def reset_prefix(self, protect: int = 2) -> None:
        """The message list was rebuilt (handoff, compaction from memory): the pinned note is gone."""
        self.protect, self._ledger_index = protect, None

    def rollback_text(self, n_dropped: int) -> str:
        t = (f"[context window] Your last {n_dropped} message(s) made the context larger than the window, so they were "
             f"undone and are no longer in your context (retry {self.retries}/{self.max_retries}; the tokens they cost "
             "are spent). Before anything else: update the memory file with what you know and what is left, then "
             "run `compact`. Then go on, with commands that print less.")
        if self.overflow_cmds:
            t += "\nCommands whose output overflowed the window (do not run them again unchanged):\n" + "\n".join(
                f"  - {c}" for c in self.overflow_cmds)
        return t

    def stats(self) -> dict[str, Any]:
        return {"window": self.window, "peak": self.peak, "rollbacks": self.retries, "dropped_messages": self.dropped_turns,
                "ratio": round(self.ratio, 3)}


def truncate_output(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    h = max_chars // 2
    return f"{text[:h]}\n[... {len(text) - 2 * h} characters left out; print less ...]\n{text[-h:]}"


def dumps_small(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))
