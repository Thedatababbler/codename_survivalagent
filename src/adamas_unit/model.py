"""The model channel (docs/USING_ADAMAS.md §3.5): one OpenAI-compatible endpoint (the local proxy), one model.

``.env`` (not committed; ``.env.example`` shows its shape) gives ``OPENAI_BASE_URL``, ``OPENAI_API_KEY`` and
``UNIT_MODEL``. The key goes into this process's environment only, where litellm reads it; it is never put in a
model config, because mini-swe-agent writes the model config into every trajectory and template variables.

- ``agent_model()``: the tool-calling model of builder, repairer and author sessions (mini-swe-agent's
  ``LitellmModel``; dollar cost tracking off: tokens are booked by the ledger).
- ``complete(messages)``: a plain completion without tools for the summariser, the budget planner and the evolver;
  returns the text and the (prompt, completion) tokens.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
_LOADED = False


def load_env(path: Path = ENV_FILE) -> None:
    """``KEY=value`` / ``export KEY=value`` lines into os.environ (the file wins over the shell)."""
    global _LOADED
    if _LOADED:
        return
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                os.environ[m.group(1)] = m.group(2).strip("'\"")   # the project's .env names the channel: it wins
    os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
    _LOADED = True


def model_name() -> str:
    load_env()
    return os.environ.get("UNIT_MODEL") or "gpt-5.6-luna"


def _base() -> str:
    load_env()
    base = os.environ.get("OPENAI_BASE_URL") or ""
    if not base:
        raise RuntimeError("OPENAI_BASE_URL is not set (.env)")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set (.env)")
    return base


def agent_model(**model_kwargs: Any):
    from minisweagent.models.litellm_model import LitellmModel

    m = LitellmModel(model_name=f"openai/{model_name()}", cost_tracking="ignore_errors",
                     model_kwargs={"api_base": _base(), **model_kwargs})
    # a missing module is not transient: mini-swe-agent would retry it 10 times with backoff (2026-11-05: litellm
    # 1.108 imports `mcp` whenever tools are passed; `mcp` is now a dependency)
    m.abort_exceptions = [*m.abort_exceptions, ImportError]
    return m


def complete(messages: list[dict[str, str]], **kwargs: Any) -> tuple[str, int, int]:
    """One plain completion: (text, prompt tokens, completion tokens)."""
    import litellm

    r = litellm.completion(model=f"openai/{model_name()}", messages=messages, api_base=_base(), **kwargs)
    u = getattr(r, "usage", None)
    p = int(getattr(u, "prompt_tokens", 0) or 0)
    c = int(getattr(u, "completion_tokens", 0) or 0)
    return str(r.choices[0].message.content or ""), p, c


SUMMARY_PROMPT = """You are compressing the earlier part of a coding session so that the session can continue in a
smaller context. Write a factual summary for the agent itself: what the task is, which files it created or changed
and what each now contains (names of functions and classes), which commands and tests it ran with what results, what
is still failing or unfinished, and what it planned to do next. No advice, no speculation; at most 600 words."""


def summarize(messages: list[dict]) -> tuple[str, int, int]:
    """The summariser of context compression (DESIGN §6; after Mini-Agent's ``_summarize_messages``)."""
    parts = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        text = str(content or "")
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            text += f"\n[command] {fn.get('arguments', '')}"
        parts.append(f"## {m.get('role')}\n{text[:4000]}")
    transcript = "\n\n".join(parts)[-120_000:]
    return complete([{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": transcript}])
