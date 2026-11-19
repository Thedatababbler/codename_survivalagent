"""Evolution of the planner's skill documents (refactor §6).

One *combination* is six skill documents. Each generation: a parent is taken from the frontier; the evolver reads
the parent's documents and the note on its runs (``note.build_note``: figures only) and rewrites at most two of
the six, with its reasons and a prediction; the rewritten documents are validated (``skills.parse_skill`` and the
sealed leak check; one return to the evolver with the problems, then dropped); the new combination runs on every
evolution task; the frontier is recomputed (``frontier.select``). Evolution stops after ``patience`` proposals in a
row that leave the frontier as it was, or when the caller's quota says so.

Running a round and scoring it are the caller's (``runner``), so that the loop itself can be driven without any
quota by the positive control. The evolver's tokens are tallied in the state, apart from every round's budget.

The loop follows the skill-optimisation loop of the Context Language Models reference implementation (propose from
contrasted runs, validate, evaluate, keep what the gate keeps); this project keeps a frontier instead of one
incumbent, compares four resources instead of one cost, and never shows the proposer code or commands.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from survival import frontier as FR
from survival.note import build_note
from survival.skills import SCHEMA, SECTIONS, SKILL_NAMES, STATE, SkillError, load_skillset, parse_skill

Complete = Callable[[list[dict]], tuple[str, int, int]]
# runner(combination id, skills dir, task, repetition) -> {"round", "usable", "ratio", "submittable", "resources", "digest"}
Runner = Callable[[str, Path, str, int], dict[str, Any]]

MAX_REWRITES = 2


def _schema_text() -> str:
    lines = []
    for n in SKILL_NAMES:
        s = SCHEMA[n]
        ps = ", ".join(f"{k}: {'|'.join(v) if isinstance(v, tuple) else v}" for k, v in s["params"].items())
        lines.append(f"- {n}: actions [{', '.join(s['actions']) or 'none: it only answers requests'}]; params {{{ps}}}")
    return "\n".join(lines)


SYSTEM = f"""You are the evolver of a resource planner. A coding agent (the executor) builds a software repository in
a fixed sequence of stages under one token budget. It may pay, out of the current stage's tokens, for three kinds of
sub-agents: a reviewer (reads, cannot write), a repairer, and a worker that takes over part of a stage in a fresh
context after the executor has written its memory file. Its context has a fixed window: a turn that would overflow
it is undone. Hard limits that you cannot change: the token budget, the floor of every later stage, the number of
compactions, the call limits of each sub-agent kind and of all together.

A planner watches the run and acts through six SKILL documents. You improve those documents; nothing else.

The planner state (the only names a condition may use; ratios are 0..1, counts are small integers):
{", ".join(f"{k} ({v})" for k, v in STATE.items())}

The six skills, the actions their triggers may fire, and their parameters:
{_schema_text()}

What the actions do: remind -> the skill's "What to do" text is sent to the executor; framework_compaction -> the
framework replaces the older turns by a summary; request_compaction -> the executor is asked to update its memory
file and compact; force_compaction -> the program compacts now (from the memory file when it is fresh and
use_memory is true, else a summary); suggest_handoff / suggest_review / suggest_repair -> the executor is advised
to call that sub-agent, with a quota of the given share of what the stage has left. A trigger fires when its
condition turns true and is armed again when it turns false. completion-check is different: it is asked each time
the executor declares a stage done (declaring the last stage submits the repository), and the first of its triggers
whose condition holds decides (none holds: accept). accept -> the declaration goes on; prompt_continue -> the
declaration is held and the executor is told to keep checking and improving, with its usage so far, until
idle_prompts_to_accept prompts in a row changed no code or max_prompts were given; require_review -> held until a
review was made in the stage; require_selftest -> held until at least one of the executor's own tests passes; a
requirement is dropped after max_refusals refusals. budget-approval has no triggers: mode
approve_within_cap grants what is asked up to max_grant_share of the mechanical cap; mode judge lets a planner
model decide from the skill's text.

A SKILL document has a YAML header (name, description, triggers: [{{when, action}}], params) and a body with exactly
these four sections: {", ".join("## " + s for s in SECTIONS)}.
"What to do" and "Checking it worked" are sent to the executor when a trigger of the skill fires; "Reading the
readout" is in the executor's instructions from the start; "Mechanism" is for the planner model.

Rules that are checked mechanically (a document that breaks one is returned to you once, then dropped):
- conditions use only the state names above, with ratios (0..1) and counts (at most 100);
- no absolute token amount anywhere; no name of a task, package, module, file or function: the documents must
  hold for tasks you have never seen;
- parameters and actions only from the lists above.

You see figures only: no code, no tests. The runs are compared first by quality (the hidden pass ratio, a
difference must exceed the noise between repeated runs) and, at equal quality, by four resources: tokens, repair
calls, compactions, peak context.

Rewrite AT MOST {MAX_REWRITES} of the six documents (each as a whole). Every reason must point at figures in the
note (a run label such as T2-R1 and what it shows). Answer with exactly these blocks and nothing else:

<<<ANALYSIS>>>
what the better runs did differently from the worse ones, with run labels; which document(s) you change and why
<<<END>>>
<<<PREDICTION>>>
{{"quality": "up|same|down", "tokens": "up|same|down", "repairs": "up|same|down", "compressions": "up|same|down", "peak_context": "up|same|down"}}
<<<END>>>
<<<SKILL name=<one of the six names>>>>
(the whole SKILL.md)
<<<END>>>
(optionally a second SKILL block)"""

_BLOCK = re.compile(r"<<<SKILL name=([^\n>]+?)>{3,}\s*\n(.*?)\n<<<END>>>", re.S)
_ANALYSIS = re.compile(r"<<<ANALYSIS>{3,}\s*\n(.*?)\n<<<END>>>", re.S)
_PRED = re.compile(r"<<<PREDICTION>{3,}\s*\n(.*?)\n<<<END>>>", re.S)


def parse_proposal(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    a, p = _ANALYSIS.search(text), _PRED.search(text)
    pred: dict[str, Any] = {}
    if p:
        try:
            pred = json.loads(p.group(1))
        except ValueError:
            pred = {"raw": p.group(1)[:300]}
    return {"analysis": a.group(1).strip() if a else "", "prediction": pred,
            "skills": [(n.strip(), b.strip() + "\n") for n, b in _BLOCK.findall(text)]}


def validate(prop: dict[str, Any], *, forbidden: set[str], leak_check: Callable[[str], bool] | None) -> tuple[dict[str, str], list[str]]:
    """The accepted rewrites {name: text} and the problems of the proposal (empty when all is accepted)."""
    problems: list[str] = []
    ok: dict[str, str] = {}
    if not prop["analysis"]:
        problems.append("the ANALYSIS block is missing or empty")
    if not prop["skills"]:
        problems.append("no SKILL block")
    if len(prop["skills"]) > MAX_REWRITES:
        problems.append(f"{len(prop['skills'])} documents rewritten; at most {MAX_REWRITES}")
    for name, text in prop["skills"][:MAX_REWRITES]:
        if name not in SKILL_NAMES:
            problems.append(f"{name}: not one of the six skills")
            continue
        if name in ok:
            problems.append(f"{name}: rewritten twice")
            continue
        try:
            parse_skill(text, name=name, forbidden=forbidden)
        except SkillError as e:
            problems += [f"{name}: {x}" for x in e.problems]
            continue
        if leak_check is not None and not leak_check(text):
            problems.append(f"{name}: shares text with sealed material")
            continue
        ok[name] = text
    return ok, problems


def user_message(skill_texts: dict[str, str], note: str, history: list[dict[str, Any]]) -> str:
    parts = ["## THE CURRENT COMBINATION (the six SKILL.md documents)"]
    for n in SKILL_NAMES:
        parts += [f"### {n}", "```", skill_texts[n].strip(), "```"]
    parts += ["", "## NOTE ON ITS RUNS (figures only)", note]
    if history:
        parts += ["## EARLIER PROPOSALS AND WHAT BECAME OF THEM", "```json", json.dumps(history[-8:], indent=1), "```"]
    parts += ["## YOUR TASK", f"Rewrite at most {MAX_REWRITES} documents. Emit the blocks of the output contract and nothing else."]
    return "\n".join(parts)


def propose(complete: Complete, skill_texts: dict[str, str], note: str, history: list[dict[str, Any]], *,
            forbidden: set[str], leak_check: Callable[[str], bool] | None) -> dict[str, Any]:
    """One proposal: at most two tries (the second with the first's problems). ``rewrites`` is empty when dropped."""
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_message(skill_texts, note, history)}]
    tokens = {"prompt": 0, "completion": 0}
    tries = []
    for attempt in (1, 2):
        text, p, c = complete(msgs)
        tokens["prompt"] += p
        tokens["completion"] += c
        prop = parse_proposal(text)
        ok, problems = validate(prop, forbidden=forbidden, leak_check=leak_check)
        tries.append({"attempt": attempt, "problems": problems, "rewritten": [n for n, _ in prop["skills"]]})
        if not problems and ok:
            return {"rewrites": ok, "analysis": prop["analysis"], "prediction": prop["prediction"], "tries": tries,
                    "tokens": tokens, "request": msgs[1]["content"]}
        msgs = [*msgs, {"role": "assistant", "content": text},
                {"role": "user", "content": "Your proposal was not accepted:\n" + "\n".join(f"- {x}" for x in problems)
                 + "\nSend the whole answer again, corrected."}]
    return {"rewrites": {}, "analysis": prop["analysis"], "prediction": prop["prediction"], "tries": tries, "tokens": tokens,
            "request": msgs[1]["content"]}


class Evolution:
    def __init__(self, out: Path, *, tasks: list[str], runner: Runner, complete: Complete, eps: dict[str, float],
                 base_skills: Path, forbidden: set[str], leak_check: Callable[[str], bool] | None = None, reps: int = 2,
                 patience: int = 5, cap: int = 8, log: Callable[[str], None] = print):
        self.out, self.tasks, self.runner, self.complete, self.eps = Path(out), list(tasks), runner, complete, dict(eps)
        self.forbidden, self.leak_check, self.reps, self.patience, self.cap, self.log = forbidden, leak_check, reps, patience, cap, log
        self.out.mkdir(parents=True, exist_ok=True)
        self.labels = {t: f"T{i + 1}" for i, t in enumerate(self.tasks)}      # the evolver never sees a task's name
        sf = self.out / "state.json"
        if sf.is_file():
            self.state = json.loads(sf.read_text())
        else:
            d = self.out / "combos" / "pi0"
            texts = load_skillset(base_skills, forbidden=forbidden).texts()
            for n, t in texts.items():
                (d / n).mkdir(parents=True, exist_ok=True)
                (d / n / "SKILL.md").write_text(t, encoding="utf-8")
            self.state = {"combos": {"pi0": {"parent": None, "generation": 0, "runs": {}, "changed": []}},
                          "frontier": ["pi0"], "generation": 0, "no_change": 0, "history": [],
                          "evolver_tokens": {"prompt": 0, "completion": 0}, "stopped": None}
            self._save()

    def _save(self) -> None:
        tmp = self.out / "state.tmp"
        tmp.write_text(json.dumps(self.state, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.out / "state.json")

    def skills_dir(self, cid: str) -> Path:
        return self.out / "combos" / cid

    # ------------------------------------------------------------------ running

    def ensure_runs(self, cid: str) -> bool:
        """Every task ``reps`` usable runs; False when a run was unusable (void): the caller stops."""
        c = self.state["combos"][cid]
        for t in self.tasks:
            runs = c["runs"].setdefault(t, [])
            while sum(1 for r in runs if r.get("usable")) < self.reps:
                r = self.runner(cid, self.skills_dir(cid), t, len(runs) + 1)
                runs.append(r)
                self._save()
                if not r.get("usable"):
                    self.state["stopped"] = {"why": "unusable run", "combination": cid, "task": t, "round": r.get("round")}
                    self._save()
                    return False
        return True

    def usable(self, cid: str) -> dict[str, list[dict[str, Any]]]:
        return {t: [r for r in rs if r.get("usable")] for t, rs in self.state["combos"][cid]["runs"].items()}

    def scores(self) -> dict[str, dict[str, Any]]:
        return {cid: FR.score(self.usable(cid)) for cid, c in self.state["combos"].items()
                if c.get("status") != "dropped" and all(len(self.usable(cid).get(t, [])) >= self.reps for t in self.tasks)}

    def reselect(self) -> dict[str, Any]:
        sel = FR.select(self.scores(), eps=self.eps, baseline="pi0", cap=self.cap)
        return sel

    # ------------------------------------------------------------------ one generation

    def pick_parent(self) -> str:
        used = {}
        for h in self.state["history"]:
            used[h["parent"]] = used.get(h["parent"], 0) + 1
        return min(self.state["frontier"], key=lambda k: (used.get(k, 0), k))

    def note_for(self, cid: str) -> str:
        return build_note({self.labels[t]: [r["digest"] for r in rs] for t, rs in self.usable(cid).items()})

    def generation(self) -> dict[str, Any]:
        g = self.state["generation"] + 1
        if not self.ensure_runs("pi0"):
            return {"generation": g, "stopped": self.state["stopped"]}
        parent = self.pick_parent()
        gdir = self.out / "generations" / f"g{g:02d}"
        gdir.mkdir(parents=True, exist_ok=True)
        texts = load_skillset(self.skills_dir(parent), forbidden=self.forbidden).texts()
        note = self.note_for(parent)
        (gdir / "note.md").write_text(note, encoding="utf-8")
        hist = [{k: h.get(k) for k in ("generation", "parent", "changed", "prediction", "outcome")} for h in self.state["history"]]
        prop = propose(self.complete, texts, note, hist, forbidden=self.forbidden, leak_check=self.leak_check)
        for k in ("prompt", "completion"):
            self.state["evolver_tokens"][k] += prop["tokens"][k]
        (gdir / "evolver_request.md").write_text(prop["request"], encoding="utf-8")
        (gdir / "analysis.md").write_text(prop["analysis"] + "\n", encoding="utf-8")
        before = list(self.state["frontier"])
        rec: dict[str, Any] = {"generation": g, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "parent": parent,
                               "changed": sorted(prop["rewrites"]), "prediction": prop["prediction"], "tries": prop["tries"],
                               "analysis": prop["analysis"][:4000], "frontier_before": before}
        if not prop["rewrites"]:
            rec["outcome"] = "dropped: not valid after one return"
        else:
            cid = f"g{g:02d}"
            new = {**texts, **prop["rewrites"]}
            for n, t in new.items():
                (self.skills_dir(cid) / n).mkdir(parents=True, exist_ok=True)
                (self.skills_dir(cid) / n / "SKILL.md").write_text(t, encoding="utf-8")
            self.state["combos"][cid] = {"parent": parent, "generation": g, "runs": {}, "changed": sorted(prop["rewrites"])}
            rec["combination"] = cid
            self._save()
            if not self.ensure_runs(cid):
                rec["outcome"] = "stopped: unusable run"
                rec["stopped"] = self.state["stopped"]
                self.state["history"].append(rec)
                self.state["generation"] = g
                self._save()
                return rec
            sel = self.reselect()
            self.state["frontier"] = sel["frontier"]
            sc = self.scores()
            rec.update(outcome="on the frontier" if cid in sel["frontier"] else "eliminated: " + sel["eliminated"].get(cid, ""),
                       score={k: sc[cid][k] for k in ("quality", "per_task", "submittable", "resources")},
                       eliminated=sel["eliminated"], noise=sel["noise"])
        rec["frontier_after"] = list(self.state["frontier"])
        changed = sorted(before) != sorted(self.state["frontier"])
        self.state["no_change"] = 0 if changed else self.state["no_change"] + 1
        self.state["history"].append(rec)
        self.state["generation"] = g
        self._save()
        (gdir / "generation.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        self.log(json.dumps({"generation": g, "parent": parent, "changed": rec["changed"], "outcome": rec["outcome"],
                             "frontier": rec["frontier_after"], "no_change": self.state["no_change"]}))
        return rec

    def run(self, max_generations: int, should_stop: Callable[[], str] = lambda: "") -> dict[str, Any]:
        while self.state["generation"] < max_generations and self.state["no_change"] < self.patience and not self.state["stopped"]:
            why = should_stop()
            if why:
                self.state["stopped"] = {"why": why}
                self._save()
                break
            self.generation()
        sc = self.scores()
        best = FR.final_pick(sc, self.state["frontier"], "pi0") if sc and all(k in sc for k in self.state["frontier"]) else None
        self.state["final_pick"] = best
        self._save()
        return {"frontier": self.state["frontier"], "final_pick": best, "generations": self.state["generation"],
                "no_change": self.state["no_change"], "stopped": self.state["stopped"],
                "evolver_tokens": self.state["evolver_tokens"]}
