"""Planner skills: the documents that drive the planner, and what evolves (refactor §4).

A skill is ``memory/planner/skills/<name>/SKILL.md``: a YAML header the program executes (``triggers``: a condition
over the planner state, the action it fires and, optionally, ``mode``: ``once`` -- the default: the trigger fires
when its condition turns true and is armed again when it turns false -- or ``while``: it fires before every call
for as long as the condition holds (user decision 2026-11-22); ``params``) and a body of four sections that is injected, part by
part, into prompts:

- ``## Mechanism``            -> the planner model's judgment calls (with the rest of the body);
- ``## What to do``           -> sent to the executor when a trigger of the skill fires;
- ``## Reading the readout``  -> the executor's system prompt (how to judge from the readout);
- ``## Checking it worked``   -> sent with ``What to do``.

Conditions may use only the planner state of §4.1 (``STATE``), written as ratios (0..1) and counts; a header or a
body may not name a task, a package, a module or a function, nor an absolute token number (``validate``). The
condition language is a small boolean expression (``and``, ``or``, ``not``, comparisons); it is parsed, never
``eval``-ed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SECTIONS = ("Mechanism", "What to do", "Reading the readout", "Checking it worked")

# §4.1: everything the planner sees. name -> kind (ratio 0..1, count, flag)
STATE: dict[str, str] = {
    "stage_tokens": "ratio",        # this stage's tokens used / its allocation
    "round_tokens": "ratio",        # the round's tokens used / B
    "context": "ratio",             # the executor's context / the window
    "compressions": "count", "compressions_limit": "count",
    "reviews": "count", "reviews_limit": "count",
    "repairs": "count", "repairs_limit": "count",
    "workers": "count", "workers_limit": "count",
    "subagents": "count", "subagents_limit": "count",
    "idle_steps": "count",          # executor steps since the code last changed
    "missing_delta": "count",       # change of the import check's missing count between the last two measurements
    "tests_delta": "count",         # change of the executor's own passing tests between the last two measurements
    "stage_progress": "ratio",      # the current stage's number / the number of stages
    "stage_done": "flag",           # every stage has been declared done (the executor is past the plan)
}
COUNT_MAX = 100                     # a count in a condition or a parameter; anything larger is an absolute amount

# What each skill may fire and which parameters it may carry: {key: kind}; kind is "ratio", "count", "flag" or a
# tuple of allowed words.
SCHEMA: dict[str, dict[str, Any]] = {
    "token-reminder": {"actions": ("remind",),
                       "params": {"advise": ("wrap_up", "request_topup", "either"), "repeat": "flag"}},
    "memory-compaction": {"actions": ("framework_compaction", "request_compaction", "force_compaction"),
                          "params": {"keep_recent_turns": "count", "use_memory": "flag"}},
    "subagent-handoff": {"actions": ("suggest_handoff",),
                         "params": {"quota_share": "ratio", "mode": ("fixed", "judge")}},
    "review-repair-dispatch": {"actions": ("suggest_review", "suggest_repair"),
                               "params": {"default_quota_share": "ratio", "review_quota_share": "ratio",
                                          "repair_quota_share": "ratio", "mode": ("fixed", "judge")}},
    "budget-approval": {"actions": (),
                        "params": {"mode": ("approve_within_cap", "judge"), "max_grant_share": "ratio"}},
    # user decision 2026-11-19: asked when the executor declares a stage done (the last declaration is the
    # submission). Its triggers are not edge-triggered: those whose condition holds are gone through in order; the first that
    # holds the declaration ends it; `accept`, or the end of the list, accepts.
    "completion-check": {"actions": ("accept", "prompt_continue", "require_review", "require_selftest"),
                         "params": {"idle_prompts_to_accept": "count", "max_prompts": "count", "max_refusals": "count",
                                    "change_after_review": "flag"}},
}
ON_DECLARATION = ("completion-check",)   # skills whose triggers are evaluated at a declaration, not before every call
SKILL_NAMES = tuple(SCHEMA)

# words of the harness itself that look like identifiers and are allowed in a body
HARNESS_WORDS = {"stage_done", "request_budget", "subagent", "compact", "AGENT_MEMORY.md", *STATE,
                 *{a for s in SCHEMA.values() for a in s["actions"]}, *{p for s in SCHEMA.values() for p in s["params"]}}


class SkillError(ValueError):
    """A skill document that the program refuses; ``problems`` lists why."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Trigger:
    when: str
    action: str
    tree: ast.Expression = field(compare=False, repr=False)
    mode: str = "once"               # "once": on the crossing; "while": before every call while the condition holds


TRIGGER_MODES = ("once", "while")


@dataclass(frozen=True)
class Skill:
    name: str
    triggers: tuple[Trigger, ...]
    params: dict[str, Any]
    sections: dict[str, str]
    text: str

    def section(self, title: str) -> str:
        return self.sections.get(title, "")


# ---------------------------------------------------------------------------------------------- conditions

_CMP = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


def _number_ok(v: Any) -> str:
    if isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return "" if -COUNT_MAX <= v <= COUNT_MAX else f"{v} is not a count (at most {COUNT_MAX}): absolute amounts are not allowed"
    if isinstance(v, float):
        return "" if 0.0 <= v <= 1.0 else f"{v} is not a ratio between 0 and 1"
    return f"{v!r} is not a number"


def parse_condition(text: str) -> tuple[ast.Expression | None, list[str]]:
    """Parse ``text``; returns (tree, problems). Allowed: state names, ratios, counts, true/false, comparisons,
    ``and`` / ``or`` / ``not``."""
    src = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", str(text)))
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        return None, [f"condition {text!r}: not an expression ({e.msg})"]
    bad: list[str] = []

    def walk(n: ast.AST) -> None:
        if isinstance(n, ast.Expression):
            walk(n.body)
        elif isinstance(n, ast.BoolOp):
            for v in n.values:
                walk(v)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.Not, ast.USub)):
            walk(n.operand)
        elif isinstance(n, ast.Compare):
            if not all(isinstance(o, _CMP) for o in n.ops):
                bad.append(f"condition {text!r}: only < <= > >= == != are allowed")
            for v in [n.left, *n.comparators]:
                walk(v)
        elif isinstance(n, ast.Name):
            if n.id not in STATE:
                bad.append(f"condition {text!r}: {n.id!r} is not part of the planner state")
        elif isinstance(n, ast.Constant):
            p = _number_ok(n.value)
            if p:
                bad.append(f"condition {text!r}: {p}")
        else:
            bad.append(f"condition {text!r}: {type(n).__name__} is not allowed")

    walk(tree)
    return (None if bad else tree), bad


def evaluate(tree: ast.Expression, state: dict[str, Any]) -> bool:
    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BoolOp):
            vals = [bool(ev(v)) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.UnaryOp):
            return (not ev(n.operand)) if isinstance(n.op, ast.Not) else -ev(n.operand)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, c in zip(n.ops, n.comparators):
                right = ev(c)
                ok = {ast.Lt: left < right, ast.LtE: left <= right, ast.Gt: left > right, ast.GtE: left >= right,
                      ast.Eq: left == right, ast.NotEq: left != right}[type(op)]
                if not ok:
                    return False
                left = right
            return True
        if isinstance(n, ast.Name):
            return state[n.id]
        return n.value  # Constant

    return bool(ev(tree))


# ---------------------------------------------------------------------------------------------- parsing

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


def split_sections(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    cur = None
    for line in body.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            cur = m.group(1)
            out[cur] = ""
        elif cur is not None:
            out[cur] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


_ABS_TOKENS = [re.compile(r"\b\d{4,}\b"), re.compile(r"\b\d+(?:\.\d+)?\s*[kKmM]\b"),
               re.compile(r"\b\d{3,}\s*(?:prompt\s+)?tokens?\b", re.I)]
_CODE_NAME = [re.compile(r"\b[\w/.-]+\.py\b"), re.compile(r"\b[A-Za-z_]\w*\(\)"), re.compile(r"\b[a-z]\w*\.[a-z_]\w*\.[a-z_]\w*\b")]


def text_problems(text: str, forbidden: set[str] | None = None) -> list[str]:
    """What a header or a body may not contain (§4.5): absolute token numbers; names of tasks, packages, modules
    and functions (``forbidden``: the names of the tasks in use, plus anything that reads as a file or a call)."""
    out = []
    for rx in _ABS_TOKENS:
        m = rx.search(text)
        if m:
            out.append(f"absolute amount {m.group(0)!r}: write ratios and counts only")
            break
    for rx in _CODE_NAME:
        for m in rx.finditer(text):
            if m.group(0) not in HARNESS_WORDS and m.group(0).rstrip("()") not in HARNESS_WORDS:
                out.append(f"{m.group(0)!r} reads as a file, module or function name")
                break
    low = text.lower()
    for name in sorted(forbidden or ()):
        if name and name not in HARNESS_WORDS and re.search(rf"(?<![\w]){re.escape(name.lower())}(?![\w])", low):
            out.append(f"names {name!r} (a task, package, module or function name)")
    return out


def parse_skill(text: str, *, name: str | None = None, forbidden: set[str] | None = None) -> Skill:
    """Parse and validate one SKILL.md; raises ``SkillError`` with every problem found."""
    problems: list[str] = []
    m = _FRONT.match(text.strip() + "\n")
    if not m:
        raise SkillError(["no YAML header between --- lines"])
    try:
        head = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError([f"header is not YAML: {e}"]) from None
    if not isinstance(head, dict):
        raise SkillError(["header is not a mapping"])
    body = m.group(2)
    nm = str(head.get("name") or "")
    if nm not in SCHEMA:
        raise SkillError([f"name {nm!r} is not one of {', '.join(SKILL_NAMES)}"])
    if name is not None and nm != name:
        problems.append(f"name {nm!r} does not match the skill being written ({name!r})")
    extra = set(head) - {"name", "description", "triggers", "params"}
    if extra:
        problems.append(f"unknown header keys: {sorted(extra)}")
    schema = SCHEMA[nm]
    triggers = []
    for t in head.get("triggers") or []:
        if not isinstance(t, dict) or set(t) - {"when", "action", "mode"} or "when" not in t or "action" not in t:
            problems.append(f"trigger {t!r}: needs 'when' and 'action' (and may have 'mode')")
            continue
        mode = t.get("mode", "once")
        if mode not in TRIGGER_MODES:
            problems.append(f"trigger mode {mode!r} is not one of {', '.join(TRIGGER_MODES)}")
            continue
        if t["action"] not in schema["actions"]:
            problems.append(f"action {t['action']!r} is not an action of {nm} ({', '.join(schema['actions']) or 'none'})")
        tree, bad = parse_condition(t["when"])
        problems += bad
        if tree is not None:
            triggers.append(Trigger(when=str(t["when"]), action=str(t["action"]), tree=tree, mode=str(mode)))
    params = head.get("params") or {}
    if not isinstance(params, dict):
        problems.append("params is not a mapping")
        params = {}
    for k, v in params.items():
        kind = schema["params"].get(k)
        if kind is None:
            problems.append(f"param {k!r} is not a parameter of {nm}")
        elif isinstance(kind, tuple):
            if v not in kind:
                problems.append(f"param {k}: {v!r} is not one of {', '.join(kind)}")
        elif kind == "flag":
            if not isinstance(v, bool):
                problems.append(f"param {k}: {v!r} is not true or false")
        elif kind == "ratio":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= float(v) <= 1.0:
                problems.append(f"param {k}: {v!r} is not a ratio between 0 and 1")
        elif kind == "count":
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= COUNT_MAX:
                problems.append(f"param {k}: {v!r} is not a count (0..{COUNT_MAX})")
    sections = split_sections(body)
    for s in SECTIONS:
        if not sections.get(s):
            problems.append(f"body section '## {s}' is missing or empty")
    problems += text_problems(str(head.get("description") or "") + "\n" + body, forbidden)
    if problems:
        raise SkillError(problems)
    return Skill(name=nm, triggers=tuple(triggers), params=dict(params), sections=sections, text=text.strip() + "\n")


# ---------------------------------------------------------------------------------------------- skill sets

@dataclass(frozen=True)
class SkillSet:
    """The skills of one combination (six since 2026-11-19: completion-check)."""
    skills: dict[str, Skill]

    def __getitem__(self, name: str) -> Skill:
        return self.skills[name]

    def param(self, skill: str, key: str, default: Any = None) -> Any:
        return self.skills[skill].params.get(key, default)

    def readout_guidance(self) -> str:
        return "\n\n".join(f"### {n}\n{self.skills[n].section('Reading the readout')}" for n in SKILL_NAMES)

    def texts(self) -> dict[str, str]:
        return {n: self.skills[n].text for n in SKILL_NAMES}


def load_skillset(root: str | Path, *, forbidden: set[str] | None = None) -> SkillSet:
    root = Path(root)
    out, problems = {}, []
    for n in SKILL_NAMES:
        f = root / n / "SKILL.md"
        if not f.is_file():
            problems.append(f"{n}: {f} is missing")
            continue
        try:
            out[n] = parse_skill(f.read_text(encoding="utf-8"), name=n, forbidden=forbidden)
        except SkillError as e:
            problems += [f"{n}: {p}" for p in e.problems]
    if problems:
        raise SkillError(problems)
    return SkillSet(out)


def write_skillset(texts: dict[str, str], root: str | Path, *, forbidden: set[str] | None = None) -> SkillSet:
    """Validate, then write (a skill is never written before it passes, §4.5)."""
    parsed = {n: parse_skill(texts[n], name=n, forbidden=forbidden) for n in SKILL_NAMES}
    root = Path(root)
    for n, s in parsed.items():
        (root / n).mkdir(parents=True, exist_ok=True)
        (root / n / "SKILL.md").write_text(s.text, encoding="utf-8")
    return SkillSet(parsed)
