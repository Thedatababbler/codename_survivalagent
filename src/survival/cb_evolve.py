"""Step D: evolution of the two context skills on ContextBench (docs/CONTEXTBENCH.md §11).

A *combination* is the six skill documents of the setting; only ``memory-compaction`` and ``token-reminder`` are
ever rewritten (``subagent-handoff`` and the other three stay as in pi_0). Each generation: a parent is taken from
the frontier (the one used least often); the evolver reads the parent's two documents and a note on its runs
(figures only: no operation text, no reply, no answer, no task name, no level, no seed) and rewrites one or both;
the rewrites are validated (``skills.parse_skill``, the rule that a skill must not tell the executor to break its
task instructions, no sealed tag; one return to the evolver with the problems, then dropped); the new combination
runs on every evolution instance; the frontier is recomputed (``cb_select.select``: compliance, then two tiers).
Evolution stops after ``patience`` proposals in a row that leave the frontier as it was, on a void run, on a run the
grader could not grade from snapshots, or when the caller says so (usage).

Running the instances is the caller's (``run_batch``), so the positive control drives the loop without quota. The
proposal format and its parser are those of the repository setting (``survival.evolve``).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from survival import cb_select as SEL
from survival.evolve import parse_proposal
from survival.skills import SCHEMA, SECTIONS, SKILL_NAMES, SkillError, load_skillset, parse_skill

Complete = Callable[[list[dict]], tuple[str, int, int]]
# run_batch(combination id, skills dir, [(family, level, seed), ...]) -> one result.json dict per run, each with
# "void" (the breach scanner's verdict) added
RunBatch = Callable[[str, Path, list[tuple[str, str, int]]], list[dict[str, Any]]]

EVOLVED = ("memory-compaction", "token-reminder")
STATE_HERE = {"context": "ratio: the executor's context / its window", "round_tokens": "ratio: the run's tokens used / its allowance",
              "compressions": "count: compactions done so far in the run"}
FORBIDDEN = {"kv_store", "log_triage", "needle_retention", "sudoku_sketchpad", "sudoku", "needle", "contextbench"}
SENT_SECTIONS = ("What to do", "Reading the readout", "Checking it worked")     # what the executor is shown


def _schema_text() -> str:
    lines = []
    for n in EVOLVED:
        s = SCHEMA[n]
        ps = ", ".join(f"{k}: {'|'.join(v) if isinstance(v, tuple) else v}" for k, v in s["params"].items())
        lines.append(f"- {n}: actions [{', '.join(s['actions'])}]; params {{{ps}}}")
    return "\n".join(lines)


def system_prompt() -> str:
    return f"""You are the evolver of a context manager. An agent (the executor) works through a long stream of
operations that arrive one at a time; the stream is several times larger than the executor's context window, and at
the end the executor is graded on what its task asked it to keep or to answer. A turn that would overflow the
window is undone. The executor works through a shell; each command's output stays in its context.

A planner watches the run and acts through SKILL documents. You improve two of them, {EVOLVED[0]} and
{EVOLVED[1]}; nothing else can be changed.

The planner state a condition may use:
{chr(10).join(f"- {k} ({v})" for k, v in STATE_HERE.items())}

The two skills, the actions their triggers may fire, and their parameters:
{_schema_text()}

What the actions do: remind -> the skill's "What to do" text is sent to the executor; framework_compaction -> the
framework replaces the older turns by a summary written by a separate model call (its tokens are charged to the
run); request_compaction -> the skill's text is sent to the executor (it can run the command `compact` itself);
force_compaction -> the program compacts now (from the executor's memory file when the run has one, it was updated
since the last compaction and use_memory is true; else a summary). A compaction keeps the newest turns as they
are: those that together take at most keep_recent_share of the window (at least the newest one) when that
parameter is given, else keep_recent_turns turns. A trigger fires when its condition turns true and is armed again
when it turns false (mode: once, the default), or before every model call for as long as its condition holds
(mode: while).

A SKILL document has a YAML header (name, description, triggers: [{{when, action, mode (optional: once | while)}}],
params) and a body with exactly these four sections: {", ".join("## " + s for s in SECTIONS)}.
"What to do" and "Checking it worked" are sent to the executor when a trigger of the skill fires; "Reading the
readout" is in the executor's instructions from the start; "Mechanism" is for you and the planner.

{SEL.evolver_rules()}

Rules that are checked mechanically (a document that breaks one is returned to you once, then dropped):
- conditions use only the state names above, with ratios (0..1) and counts (at most 100);
- no absolute token amount anywhere; no name of a task, file, module or function: the documents must hold for
  tasks you have never seen;
- parameters and actions only from the lists above;
- a sentence that tells the executor to write, save or store something in a file, on disk, in the memory file or
  anywhere outside its context must carry the condition that its task allows it; no sentence may say that the
  task's instructions can be set aside.

You see figures only. Combinations are compared first by quality (the mean score over the tasks; a difference
must exceed the noise between repeated runs) and, at equal quality, by four resources: tokens, compactions,
roll-backs, peak context. A combination whose runs break their task's instructions more often than the baseline's
is discarded whatever it scores.

Rewrite one or both documents (each as a whole). Every reason must point at figures in the note (a run label such
as T1-I2 and what it shows). Answer with exactly these blocks and nothing else:

<<<ANALYSIS>>>
what the better runs did differently from the worse ones, with run labels; which document(s) you change and why
<<<END>>>
<<<PREDICTION>>>
{{"quality": "up|same|down", "tokens": "up|same|down", "compactions": "up|same|down", "rollbacks": "up|same|down", "peak_context": "up|same|down"}}
<<<END>>>
<<<SKILL name=<one of the two names>>>>
(the whole SKILL.md)
<<<END>>>
(optionally a second SKILL block)"""


# ---------------------------------------------------------------------------------------------- validation

_WRITE = r"\b(?:write|save|store|record|append|dump|copy|log|persist|jot|note\s+down|put|keep|move|offload|park|stash)\b"
_PLACE = (r"\b(?:files?|disk|memory\s+file|AGENT_MEMORY(?:\.md)?|notes?\s+file|scratch\s*(?:file|pad|space)?|"
          r"outside\s+(?:of\s+)?(?:your|the)\s+context|working\s+directory|filesystem|file\s+system)\b")
_ALLOWED = (r"(?:task(?:'s)?(?:\s+instructions?)?\s+(?:allows?|permits?|lets?)|(?:allowed|permitted)\s+by\s+(?:your|the)\s+task|"
            r"unless\s+(?:your|the)\s+task|(?:your|the)\s+task\s+does\s+not\s+forbid|if\s+(?:your|the)\s+task)")
_OVERRIDE = (r"(?:ignore|disregard|override|set\s+aside|regardless\s+of|despite|in\s+spite\s+of|even\s+(?:if|when|though))"
             r"[^.\n]{0,80}\b(?:task|instructions?)\b|\b(?:task|instructions?)\b[^.\n]{0,40}(?:can|may|should)\s+be\s+(?:ignored|set\s+aside|overridden)")


def instruction_problems(text: str) -> list[str]:
    """The mechanical form of "a skill must not tell the executor to break its task instructions" (user decisions
    2026-11-22, 2026-11-23): in what the executor is shown, a sentence that tells it to put something in a file, on
    disk, in the memory file or outside its context must carry the condition that the task allows it; and no
    sentence may say that the task's instructions can be set aside. A heuristic on wording: what it lets through is
    still measured in the runs (the compliance figure) and discarded there."""
    out = []
    for sent in re.split(r"(?<=[.!?;:])\s+|\n+", text):
        s = sent.strip()
        if not s:
            continue
        if re.search(_OVERRIDE, s, re.I):
            out.append(f"says the task's instructions can be set aside: {s[:120]!r}")
        elif re.search(_WRITE, s, re.I) and re.search(_PLACE, s, re.I) and not re.search(_ALLOWED, s, re.I):
            out.append(f"tells the executor to write outside its context without the condition that its task allows it: {s[:120]!r}")
    return out


_TAG = re.compile(r"#[0-9a-f]{8}\b")


def validate(prop: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    problems: list[str] = []
    ok: dict[str, str] = {}
    if not prop["analysis"]:
        problems.append("the ANALYSIS block is missing or empty")
    if not prop["skills"]:
        problems.append("no SKILL block")
    if len(prop["skills"]) > len(EVOLVED):
        problems.append(f"{len(prop['skills'])} documents rewritten; at most {len(EVOLVED)}")
    for name, text in prop["skills"][:len(EVOLVED)]:
        if name not in EVOLVED:
            problems.append(f"{name}: only {' and '.join(EVOLVED)} may be rewritten")
            continue
        if name in ok:
            problems.append(f"{name}: rewritten twice")
            continue
        try:
            sk = parse_skill(text, name=name, forbidden=FORBIDDEN)
        except SkillError as e:
            problems += [f"{name}: {x}" for x in e.problems]
            continue
        bad = instruction_problems("\n".join(sk.section(s) for s in SENT_SECTIONS))
        if _TAG.search(text):
            bad.append("holds a tag of the kind the operations carry")
        if bad:
            problems += [f"{name}: {x}" for x in bad]
            continue
        ok[name] = text
    return ok, problems


def user_message(texts: dict[str, str], note: str, history: list[dict[str, Any]]) -> str:
    parts = ["## THE CURRENT COMBINATION (the two SKILL.md documents you may rewrite)"]
    for n in EVOLVED:
        parts += [f"### {n}", "```", texts[n].strip(), "```"]
    parts += ["", "## NOTE ON ITS RUNS (figures only)", note]
    if history:
        parts += ["## EARLIER PROPOSALS AND WHAT BECAME OF THEM", "```json", json.dumps(history[-8:], indent=1), "```"]
    parts += ["## YOUR TASK", "Rewrite one or both documents. Emit the blocks of the output contract and nothing else."]
    return "\n".join(parts)


def propose(complete: Complete, texts: dict[str, str], note: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    """One proposal: at most two tries (the second with the first's problems). ``rewrites`` is empty when dropped."""
    msgs = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user_message(texts, note, history)}]
    tokens, tries = {"prompt": 0, "completion": 0}, []
    prop: dict[str, Any] = {"analysis": "", "prediction": {}}
    for attempt in (1, 2):
        text, p, c = complete(msgs)
        tokens["prompt"] += p
        tokens["completion"] += c
        prop = parse_proposal(text)
        ok, problems = validate(prop)
        tries.append({"attempt": attempt, "problems": problems, "rewritten": [n for n, _ in prop["skills"]]})
        if not problems and ok:
            return {"rewrites": ok, "analysis": prop["analysis"], "prediction": prop["prediction"], "tries": tries,
                    "tokens": tokens, "request": msgs[1]["content"]}
        msgs = [*msgs, {"role": "assistant", "content": text},
                {"role": "user", "content": "Your proposal was not accepted:\n" + "\n".join(f"- {x}" for x in problems)
                 + "\nSend the whole answer again, corrected."}]
    return {"rewrites": {}, "analysis": prop["analysis"], "prediction": prop["prediction"], "tries": tries, "tokens": tokens,
            "request": msgs[1]["content"]}


# ---------------------------------------------------------------------------------------------- the note

def run_figures(r: dict[str, Any]) -> dict[str, Any]:
    """What the loop keeps of a run: the score, how it ended, usage, counts, compliance and the figures-only
    digest. ``r`` is a result.json dict (with the scanner's ``void``)."""
    t, c = r["tokens"], r["counts"]
    events = [{k: e[k] for k in ("event", "call", "ops", "why", "from_memory", "dropped", "kept_turns", "kept_tokens", "summary_share") if k in e}
              for e in r.get("events") or []]
    fired = [{"skill": f["skill"], "action": f["action"], "context": (f.get("state") or f).get("context"), "call": f.get("tick")}
             for f in r.get("planner_fired") or []]
    window = r["context"]["window"]
    return {"run": r["run"], "family": r["family"], "level": r["level"], "seed": r["seed"], "reward": float(r["reward"]),
            "usable": not r.get("void") and r.get("graded_from") == "snapshots" and r.get("grader_valid") == 1.0,
            "void": bool(r.get("void")), "graded_from": r.get("graded_from"), "exit_status": r["exit_status"],
            "terminated_by": r["terminated_by"], "compliance": r["compliance"],
            "resources": {"tokens": t["prompt"] + t["completion"], "compactions": c["compactions"], "rollbacks": c["rollbacks"],
                          "peak_context": round(r["context"]["peak_o200k"] / window, 4)},
            "figures": {"score": round(float(r["reward"]), 4), "ended": r["exit_status"],
                        "operations_delivered": f"{r['ops_delivered']}/{r['ops_total']}",
                        "prompt_tokens": t["prompt"], "of_which_summary_calls": t["summary_prompt"], "completion_tokens": t["completion"],
                        "cached_share_of_prompt": round(t["cached"] / t["prompt"], 3) if t["prompt"] else 0.0,
                        "model_calls": c["calls"], "compactions": c["compactions"], "compactions_from_memory_file": c["compactions_from_memory"],
                        "rollbacks": c["rollbacks"], "reminders": c["reminders"], "replies_without_command": c["free_turns"],
                        "peak_context_share": round(r["context"]["peak_o200k"] / window, 3),
                        "task_forbids_disk": r["compliance"]["disk_forbidden_by_task"], "broke_task_instructions": r["compliance"]["violation"],
                        "planner_actions": fired[:80], "events": events[:120],
                        "context_share_by_call": [round(x["context"] / window, 2) for x in r.get("trace") or []][:400]}}


def build_note(runs: dict[str, list[dict[str, Any]]], labels: dict[str, str]) -> str:
    """``runs``: {instance key: [run figures]}; ``labels``: {instance key: "T1-I2"}. Nothing here names a task, a
    level or a seed."""
    rows = ["| run | score | ended | prompt tokens | summary share | calls | compactions | roll-backs | reminders | peak context | broke task instructions |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    flat = []
    for key in sorted(runs, key=lambda k: labels[k]):
        for i, r in enumerate(runs[key]):
            f = r["figures"]
            lab = labels[key] + (f"-R{i + 1}" if len(runs[key]) > 1 else "")
            flat.append((lab, labels[key].split("-")[0], f))
            rows.append(f"| {lab} | {f['score']:.3f} | {f['ended']} | {f['prompt_tokens']} | "
                        f"{(f['of_which_summary_calls'] / f['prompt_tokens'] if f['prompt_tokens'] else 0):.2f} | {f['model_calls']} | {f['compactions']} | "
                        f"{f['rollbacks']} | {f['reminders']} | {f['peak_context_share']:.2f} | {'yes' if f['broke_task_instructions'] else 'no'} |")
    parts = ["T<n> is a task, I<n> an instance of it (same task, another stream).", "", *rows, ""]
    for task in sorted({t for _, t, _ in flat}):
        mine = sorted([(lab, f) for lab, t, f in flat if t == task], key=lambda x: x[1]["score"])
        worst, best = mine[0], mine[-1]
        parts += [f"### {task}: the run with the lowest score ({worst[0]}) and the one with the highest ({best[0]})", "```json",
                  json.dumps({worst[0]: worst[1]} if worst[0] == best[0] else {worst[0]: worst[1], best[0]: best[1]}, separators=(",", ":")), "```", ""]
    return "\n".join(parts)


# ---------------------------------------------------------------------------------------------- the loop

class CBEvolution:
    def __init__(self, out: Path, *, instances: list[tuple[str, str, int]], run_batch: RunBatch, complete: Complete,
                 eps: dict[str, float], base_skills: Path, baseline_runs: list[dict[str, Any]] | None = None,
                 patience: int = 5, cap: int = 8, log: Callable[[str], None] = print):
        """``instances``: the evolution set [(family, level, seed)]. ``eps``: {family: eps} from the calibration.
        ``baseline_runs``: result.json dicts of pi_0 runs already made on these instances (the calibration's)."""
        self.out, self.instances, self.run_batch, self.complete = Path(out), [(f, lv, int(s)) for f, lv, s in instances], run_batch, complete
        self.eps, self.patience, self.cap, self.log = dict(eps), patience, cap, log
        self.out.mkdir(parents=True, exist_ok=True)
        fams = sorted({f for f, _, _ in self.instances})
        per: dict[str, int] = {}
        self.labels: dict[str, str] = {}
        for f, lv, s in self.instances:                  # the evolver never sees a task's name, level or seed
            per[f] = per.get(f, 0) + 1
            self.labels[self.key(f, lv, s)] = f"T{fams.index(f) + 1}-I{per[f]}"
        sf = self.out / "state.json"
        if sf.is_file():
            self.state = json.loads(sf.read_text())
            return
        texts = load_skillset(base_skills, forbidden=FORBIDDEN).texts()
        for n, t in texts.items():
            (self.skills_dir("pi0") / n).mkdir(parents=True, exist_ok=True)
            (self.skills_dir("pi0") / n / "SKILL.md").write_text(t, encoding="utf-8")
        self.state = {"combos": {"pi0": {"parent": None, "generation": 0, "runs": {}, "changed": []}}, "frontier": ["pi0"],
                      "generation": 0, "no_change": 0, "history": [], "evolver_tokens": {"prompt": 0, "completion": 0},
                      "run_tokens": 0, "stopped": None}
        keys = set(self.labels)
        for r in baseline_runs or []:
            k = self.key(r["family"], r["level"], r["seed"])
            if k in keys and r.get("arm") == "pi0":
                self.state["combos"]["pi0"]["runs"].setdefault(k, []).append(run_figures(r))
        self._save()

    @staticmethod
    def key(family: str, level: str, seed: int) -> str:
        return f"{family}:{level}:{int(seed)}"

    def _save(self) -> None:
        tmp = self.out / "state.tmp"
        tmp.write_text(json.dumps(self.state, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.out / "state.json")

    def skills_dir(self, cid: str) -> Path:
        return self.out / "combos" / cid

    # ------------------------------------------------------------------ running

    def ensure_runs(self, cid: str) -> bool:
        """One usable run on every evolution instance; False when a run was void or could not be graded from
        snapshots (the caller stops)."""
        c = self.state["combos"][cid]
        todo = [(f, lv, s) for f, lv, s in self.instances if not any(r["usable"] for r in c["runs"].get(self.key(f, lv, s), []))]
        if not todo:
            return True
        for r in self.run_batch(cid, self.skills_dir(cid), todo):
            fig = run_figures(r)
            c["runs"].setdefault(self.key(fig["family"], fig["level"], fig["seed"]), []).append(fig)
            self.state["run_tokens"] += fig["resources"]["tokens"]
            if not fig["usable"] and not self.state["stopped"]:
                self.state["stopped"] = {"why": "void run" if fig["void"] else "a run was not graded from snapshots",
                                         "combination": cid, "run": fig["run"]}
        self._save()
        if self.state["stopped"]:
            return False
        missing = [k for k in self.labels if not any(r["usable"] for r in c["runs"].get(k, []))]
        if missing:
            self.state["stopped"] = {"why": "runs missing after the batch", "combination": cid, "instances": len(missing)}
            self._save()
            return False
        return True

    def usable(self, cid: str) -> dict[str, list[dict[str, Any]]]:
        return {k: [r for r in rs if r["usable"]] for k, rs in self.state["combos"][cid]["runs"].items()}

    def complete_combos(self) -> list[str]:
        return [cid for cid, c in self.state["combos"].items()
                if c.get("status") != "dropped" and all(self.usable(cid).get(k) for k in self.labels)]

    def scores(self) -> dict[str, dict[str, Any]]:
        return {cid: SEL.score(self.usable(cid)) for cid in self.complete_combos()}

    # ------------------------------------------------------------------ one generation

    def pick_parent(self) -> str:
        used: dict[str, int] = {}
        for h in self.state["history"]:
            used[h["parent"]] = used.get(h["parent"], 0) + 1
        return min(self.state["frontier"], key=lambda k: (used.get(k, 0), k))

    def generation(self) -> dict[str, Any]:
        g = self.state["generation"] + 1
        if not self.ensure_runs("pi0"):
            return {"generation": g, "stopped": self.state["stopped"]}
        parent = self.pick_parent()
        gdir = self.out / "generations" / f"g{g:02d}"
        gdir.mkdir(parents=True, exist_ok=True)
        texts = load_skillset(self.skills_dir(parent), forbidden=FORBIDDEN).texts()
        note = build_note(self.usable(parent), self.labels)
        (gdir / "note.md").write_text(note, encoding="utf-8")
        hist = [{k: h.get(k) for k in ("generation", "parent", "changed", "prediction", "outcome")} for h in self.state["history"]]
        prop = propose(self.complete, texts, note, hist)
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
            for n, t in {**texts, **prop["rewrites"]}.items():
                (self.skills_dir(cid) / n).mkdir(parents=True, exist_ok=True)
                (self.skills_dir(cid) / n / "SKILL.md").write_text(t, encoding="utf-8")
            self.state["combos"][cid] = {"parent": parent, "generation": g, "runs": {}, "changed": sorted(prop["rewrites"])}
            rec["combination"] = cid
            self._save()
            if not self.ensure_runs(cid):
                rec.update(outcome="stopped: " + self.state["stopped"]["why"], stopped=self.state["stopped"])
                self.state["history"].append(rec)
                self.state["generation"] = g
                self._save()
                (gdir / "generation.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
                self.log(json.dumps({"generation": g, "parent": parent, "changed": rec["changed"], "outcome": rec["outcome"]}))
                return rec
            sc = self.scores()
            sel = SEL.select(sc, eps=self.eps, baseline="pi0", cap=self.cap)
            self.state["frontier"] = sel["frontier"]
            rec.update(outcome="on the frontier" if cid in sel["frontier"] else "eliminated: " + sel["eliminated"].get(cid, ""),
                       score={k: sc[cid][k] for k in ("quality", "per_task", "violation_rate", "resources")},
                       baseline={k: sc["pi0"][k] for k in ("quality", "per_task", "violation_rate", "resources")},
                       eliminated=sel["eliminated"], noise=sel["noise"])
        rec["frontier_after"] = list(self.state["frontier"])
        changed = sorted(before) != sorted(self.state["frontier"])
        self.state["no_change"] = 0 if changed else self.state["no_change"] + 1
        self.state["history"].append(rec)
        self.state["generation"] = g
        self._save()
        (gdir / "generation.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        self.log(json.dumps({"generation": g, "parent": parent, "changed": rec["changed"], "outcome": rec["outcome"],
                             "score": rec.get("score"), "frontier": rec["frontier_after"], "no_change": self.state["no_change"],
                             "run_tokens": self.state["run_tokens"], "evolver_tokens": self.state["evolver_tokens"]}))
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
        best = SEL.final_pick(sc, self.state["frontier"], "pi0") if sc and all(k in sc for k in self.state["frontier"]) else None
        self.state["final_pick"] = best
        self._save()
        return {"frontier": self.state["frontier"], "final_pick": best, "generations": self.state["generation"],
                "no_change": self.state["no_change"], "stopped": self.state["stopped"],
                "run_tokens": self.state["run_tokens"], "evolver_tokens": self.state["evolver_tokens"]}
