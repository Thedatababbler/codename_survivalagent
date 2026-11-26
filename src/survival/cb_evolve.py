"""Step D: evolution of the two context skills on ContextBench (docs/CONTEXTBENCH.md §11).

A *combination* is the six skill documents of the setting; only ``memory-compaction`` and ``token-reminder`` are
ever rewritten (``subagent-handoff`` and the other three stay as in pi_0). Each generation: a parent is taken from
the frontier (the one used least often); the evolver reads the parent's two documents and a note on its runs
(figures only: no operation text, no reply, no answer, no task name, no level, no seed) and rewrites one or both;
the rewrites are validated (``skills.parse_skill``, every section has a reader and the summary section is worded
generally (``section_problems``), the rule that a skill must not tell the executor to break its
task instructions, no sealed tag; one return to the evolver with the problems, then dropped); the new combination
is evaluated in two steps (user decision 2026-11-25): it first runs only the instances of the selection task; if it
is better than pi_0 there (``cb_select.better_than_baseline``) and enters the frontier on those runs, it also runs
the instances of the check task, where its mean must not be below the floor (pi_0's mean minus that task's eps);
then the frontier is recomputed (``cb_select.select_staged``: compliance, quality on the selection task, the four
resources). Evolution stops after ``patience`` proposals in a row that leave the frontier as it was, on a void run,
on a run the grader could not grade from snapshots, on a run that used clearly more than pi_0's runs of its task,
when a batch of runs stopped, or when the caller says so (usage).

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
from survival.cb_run import KEEP_SECTION
from survival.evolve import parse_proposal
from survival.skills import SCHEMA, SECTIONS, SKILL_NAMES, SkillError, load_skillset, parse_skill

Complete = Callable[[list[dict]], tuple[str, int, int]]
# run_batch(combination id, skills dir, [(family, level, seed), ...]) -> (one result.json dict per run, each with
# "void" and "flagged" (the breach scanner's verdicts) added; why the batch stopped early, '' when it did not)
RunBatch = Callable[[str, Path, list[tuple[str, str, int]]], tuple[list[dict[str, Any]], str]]

EVOLVED = ("memory-compaction", "token-reminder")
STATE_HERE = {"context": "ratio: the executor's context / its window", "round_tokens": "ratio: the run's tokens used / its allowance",
              "compressions": "count: compactions done so far in the run"}
FORBIDDEN = {"kv_store", "log_triage", "needle_retention", "sudoku_sketchpad", "sudoku", "needle", "contextbench"}
SENT_SECTIONS = ("What to do", "Reading the readout", "Checking it worked")     # what the executor is shown
# Where each part of a document goes at run time (the delivery audit of 2026-11-26, docs/CONTEXTBENCH.md §12).
# Sections a document may carry: the four of every skill, and KEEP_SECTION in memory-compaction only (required).
ALLOWED_SECTIONS = {"memory-compaction": (*SECTIONS, KEEP_SECTION), "token-reminder": SECTIONS}
NO_EFFECT_PARAMS = {"token-reminder": ("advise",)}      # accepted by the schema, read by no code of this setting


def _schema_text() -> str:
    lines = []
    for n in EVOLVED:
        s = SCHEMA[n]
        ps = ", ".join(f"{k}: {'|'.join(v) if isinstance(v, tuple) else v}" for k, v in s["params"].items()
                       if k not in NO_EFFECT_PARAMS.get(n, ()))
        lines.append(f"- {n}: actions [{', '.join(s['actions'])}]; params {{{ps}}}")
    return "\n".join(lines)


def system_prompt(select_label: str = "T2", check_label: str = "T1", gate: dict[str, Any] | None = None) -> str:
    g = gate or {}
    ref = (f" (baseline mean {g['pi0']:.3f}; mean without any context management {g['none']:.3f}; margin {g['margin']:.2f})"
           if g.get("none") is not None and "pi0" in g else "")
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

What the actions do: remind -> the skill's text for the executor is sent to it; framework_compaction -> the
framework replaces the older turns by a summary written by a separate model call (its tokens are charged to the
run), then sends the executor the skill's text; request_compaction -> the skill's text is sent to the executor (it
can run the command `compact` itself, which also compacts by a summary); force_compaction -> the program compacts
now (from the executor's memory file when the run has one, it was updated since the last compaction and use_memory
is true; else a summary), then sends the executor the skill's text. A compaction keeps the newest turns as they
are: those that together take at most keep_recent_share of the window (at least the newest one) when that
parameter is given, else keep_recent_turns turns. A trigger fires when its condition turns true and is armed again
when it turns false (mode: once, the default), or before every model call for as long as its condition holds
(mode: while).

A SKILL document has a YAML header (name, description, triggers: [{{when, action, mode (optional: once | while)}}],
params) and a body of sections: {", ".join("## " + s for s in SECTIONS)}; {EVOLVED[0]} has
one more, "## {KEEP_SECTION}". No other section is accepted.

Where each part goes at run time -- a part reaches only the reader named here, and nothing else of the document
reaches any model:
- header `triggers` and `params`: read by the program only (when it acts, how much it keeps). Never shown to a model.
- header `name`: the executor sees it as a label of the skill's texts. `description`: reaches no model.
- "## Mechanism": reaches no model at run time. It is your own note.
- "## Reading the readout": part of the executor's instructions from the first call on (both documents).
- "## What to do" and "## Checking it worked": sent to the executor as one message each time a trigger of that
  document fires. With framework_compaction and force_compaction that is AFTER the compaction is done: the
  executor reads it when the older turns are already replaced, so it cannot change what the summary holds. It is
  not sent when the program compacts without a trigger (after an undone turn) or when the executor runs `compact`.
- "## {KEEP_SECTION}" ({EVOLVED[0]} only): this text, whole and alone, is the instruction (system message) of
  the separate model call that writes the summary, at every compaction by summary (trigger, forced, or `compact`).
  That call is then given the executor's task instructions and the turns to be replaced, and its reply replaces
  those turns. The executor never sees this section; the summary writer sees nothing else of the document. Changing
  this section changes directly what a summary holds and so what is left in the executor's context afterwards.
  It must be worded generally: no concrete marker or line format, no task name, no word that belongs to one
  particular task; refer to "what the task instructions require to be kept" and the like.

{SEL.evolver_rules()}

Rules that are checked mechanically (a document that breaks one is returned to you once, then dropped):
- conditions use only the state names above, with ratios (0..1) and counts (at most 100);
- no absolute token amount anywhere; no name of a task, file, module or function: the documents must hold for
  tasks you have never seen;
- parameters and actions only from the lists above; sections only those named above;
- "## {KEEP_SECTION}" holds no marker or line format (no angle-bracket or square-bracket tokens, no `#` tags,
  nothing in backticks, no words in capitals taken from a task), and no task name or task-specific term;
- a sentence that tells the executor to write, save or store something in a file, on disk, in the memory file or
  anywhere outside its context must carry the condition that its task allows it; no sentence may say that the
  task's instructions can be set aside.

You see figures only. A new combination is judged in two steps. First it runs only the instances of task
{select_label}. It counts as better than the baseline only if every one of those runs scores above the baseline's
mean, its own mean is higher by a fixed margin, and its mean is above that of a run without any context
management{ref}. Only then it runs task {check_label}, where its mean must not fall below the baseline's by more
than the noise between repeated runs. Combinations that pass are compared by their quality on {select_label} and,
at equal quality, by four resources: tokens, compactions, roll-backs, peak context. A combination whose runs
break their task's instructions more often than the baseline's is discarded whatever it scores.

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

# "## What to keep when compacting" must be worded generally (user decision 2026-11-26): no concrete marker format,
# no task name, no term of one task. Mechanical and therefore a heuristic on wording.
_FORMATS = [(re.compile(r"<<<|>>>|<[^<>\n]{1,40}>"), "an angle-bracket token"), (re.compile(r"\[[^\]\n]{0,60}\]"), "a square-bracket token"),
            (re.compile(r"#\w"), "a `#` tag"), (re.compile(r"`"), "a literal in backticks"),
            (re.compile(r"\(\s*\w+\s*,\s*\w+\s*,"), "a tuple format"), (re.compile(r"\b\w+=\S"), "a `name=value` format"),
            (re.compile(r"\b[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)+\b"), "a marker word in capitals"),
            (re.compile(r"\b(?:NEEDLES?|FILLER|SET|GET|QUERY|ANSWER|VERSION|SKETCHPAD|BEGIN|END|START|LEVEL|BLOCK|BATCH)\b"), "a marker word in capitals")]
_TASK_TERMS = ("needle", "filler", "haystack", "sudoku", "sketchpad", "board", "grid", "cell", "tuple", "puzzle", "digit",
               "kv", "key-value", "key/value", "key value", "keys", "batch", "chunk", "triage", "incident", "query",
               "queries", "timestamp", "sentinel", "log line", "log entry", "log record", "logs", "service log")


def summary_section_problems(text: str) -> list[str]:
    out = []
    for rx, what in _FORMATS:
        m = rx.search(text)
        if m:
            out.append(f"'## {KEEP_SECTION}' holds {what} ({m.group(0)[:30]!r}): word it generally, without any concrete format")
    low = text.lower()
    for t in _TASK_TERMS:
        m = re.search(rf"(?<![\w]){re.escape(t)}(?:s|es)?(?![\w])", low)
        if m:
            out.append(f"'## {KEEP_SECTION}' uses the task-specific term {m.group(0)!r}: refer to what the task instructions require instead")
    return out


def section_problems(sk: Any) -> list[str]:
    """Every part of a document must have a reader: no section other than the allowed ones; the summary call's
    instruction present in memory-compaction and worded generally."""
    out = [f"section '## {t}' is not one of this document's sections and would reach nobody" for t in sk.sections
           if t not in ALLOWED_SECTIONS[sk.name]]
    if KEEP_SECTION in ALLOWED_SECTIONS[sk.name]:
        if not sk.section(KEEP_SECTION):
            out.append(f"section '## {KEEP_SECTION}' is missing or empty: the summary call would have no instruction")
        else:
            out += summary_section_problems(sk.section(KEEP_SECTION))
            if re.search(_OVERRIDE, sk.section(KEEP_SECTION), re.I):
                out.append(f"'## {KEEP_SECTION}' says the task's instructions can be set aside")
    return out


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
        bad = instruction_problems("\n".join(sk.section(s) for s in SENT_SECTIONS)) + section_problems(sk)
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


def propose(complete: Complete, texts: dict[str, str], note: str, history: list[dict[str, Any]], system: str = "") -> dict[str, Any]:
    """One proposal: at most two tries (the second with the first's problems). ``rewrites`` is empty when dropped."""
    msgs = [{"role": "system", "content": system or system_prompt()}, {"role": "user", "content": user_message(texts, note, history)}]
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
            "void": bool(r.get("void")), "flagged": bool(r.get("flagged")), "graded_from": r.get("graded_from"), "exit_status": r["exit_status"],
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
                 gate: dict[str, Any], base_skills: Path, baseline_runs: list[dict[str, Any]] | None = None,
                 patience: int = 5, cap: int = 8, usage_factor: float = 2.0, log: Callable[[str], None] = print):
        """``instances``: the evolution set [(family, level, seed)] -- those of the selection task and those of the
        check task. ``gate`` (figures of the calibration): {"select": {"family", "pi0": pi_0's mean, "margin",
        "none": the mean without management, "eps"}, "check": {"family", "floor": pi_0's mean minus eps}}.
        ``baseline_runs``: result.json dicts of pi_0 runs already made on these instances (the calibration's).
        ``usage_factor``: a run that used more than this many times the mean of pi_0's runs of its task stops the
        evolution."""
        self.out, self.instances, self.run_batch, self.complete = Path(out), [(f, lv, int(s)) for f, lv, s in instances], run_batch, complete
        self.gate, self.patience, self.cap, self.usage_factor, self.log = gate, patience, cap, usage_factor, log
        self.select_family, self.check_family = gate["select"]["family"], gate["check"]["family"]
        self.should_stop: Callable[[], str] = lambda: ""
        self.out.mkdir(parents=True, exist_ok=True)
        fams = sorted({f for f, _, _ in self.instances})
        if sorted({self.select_family, self.check_family}) != fams:
            raise ValueError("the evolution set must hold instances of the selection task and of the check task, and no other")
        per: dict[str, int] = {}
        self.labels: dict[str, str] = {}
        for f, lv, s in self.instances:                  # the evolver never sees a task's name, level or seed
            per[f] = per.get(f, 0) + 1
            self.labels[self.key(f, lv, s)] = f"T{fams.index(f) + 1}-I{per[f]}"
        self.task_label = {f: f"T{fams.index(f) + 1}" for f in fams}
        sf = self.out / "state.json"
        if sf.is_file():
            self.state = json.loads(sf.read_text())
            return
        texts = load_skillset(base_skills, forbidden=FORBIDDEN).texts()
        for n, t in texts.items():
            (self.skills_dir("pi0") / n).mkdir(parents=True, exist_ok=True)
            (self.skills_dir("pi0") / n / "SKILL.md").write_text(t, encoding="utf-8")
        self.state = {"combos": {"pi0": {"parent": None, "generation": 0, "runs": {}, "changed": [], "status": "baseline"}},
                      "frontier": ["pi0"], "generation": 0, "no_change": 0, "history": [],
                      "evolver_tokens": {"prompt": 0, "completion": 0}, "run_tokens": 0, "stopped": None}
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

    def of(self, family: str) -> list[tuple[str, str, int]]:
        return [i for i in self.instances if i[0] == family]

    def used_tokens(self) -> int:
        return self.state["run_tokens"] + sum(self.state["evolver_tokens"].values())

    # ------------------------------------------------------------------ running

    def _stop(self, **why: Any) -> bool:
        if not self.state["stopped"]:
            self.state["stopped"] = why
        self._save()
        return False

    def ensure_runs(self, cid: str, family: str) -> bool:
        """One usable run on every evolution instance of ``family``; False when the evolution has to stop (a void
        run, a run not graded from snapshots, a batch that stopped, usage clearly above pi_0's, the caller's cap)."""
        c = self.state["combos"][cid]
        insts = self.of(family)
        todo = [(f, lv, s) for f, lv, s in insts if not any(r["usable"] for r in c["runs"].get(self.key(f, lv, s), []))]
        if not todo:
            return True
        why = self.should_stop()
        if why:
            return self._stop(why=why, combination=cid)
        results, batch_stop = self.run_batch(cid, self.skills_dir(cid), todo)
        ref = [r["resources"]["tokens"] for k, rs in self.state["combos"]["pi0"]["runs"].items() if k.startswith(family + ":") for r in rs if r["usable"]]
        for r in results:
            fig = run_figures(r)
            c["runs"].setdefault(self.key(fig["family"], fig["level"], fig["seed"]), []).append(fig)
            self.state["run_tokens"] += fig["resources"]["tokens"]
            if not fig["usable"]:
                self._stop(why="void run" if fig["void"] else "a run was not graded from snapshots", combination=cid, run=fig["run"])
            elif cid != "pi0" and ref and fig["resources"]["tokens"] > self.usage_factor * (sum(ref) / len(ref)):
                self._stop(why=f"a run used more than {self.usage_factor:g} times the mean of the baseline's runs of its task "
                               f"({fig['resources']['tokens']} against {sum(ref) / len(ref):.0f})", combination=cid, run=fig["run"])
        self._save()
        if batch_stop:
            self._stop(why="the batch of runs stopped: " + batch_stop, combination=cid)
        if self.state["stopped"]:
            return False
        missing = [k for k in (self.key(*i) for i in insts) if not any(r["usable"] for r in c["runs"].get(k, []))]
        if missing:
            return self._stop(why="runs missing after the batch", combination=cid, instances=len(missing))
        return True

    def usable(self, cid: str, family: str = "") -> dict[str, list[dict[str, Any]]]:
        return {k: [r for r in rs if r["usable"]] for k, rs in self.state["combos"][cid]["runs"].items()
                if not family or k.startswith(family + ":")}

    def rewards(self, cid: str, family: str) -> list[float]:
        """The mean score of each instance of ``family`` (in the order of the evolution set)."""
        u = self.usable(cid, family)
        return [sum(r["reward"] for r in u[k]) / len(u[k]) for k in (self.key(*i) for i in self.of(family)) if u.get(k)]

    def pool(self) -> list[str]:
        """pi_0 and every combination that passed both steps (frontier members and those selected out later)."""
        return [cid for cid, c in self.state["combos"].items() if c.get("status") in ("baseline", "qualified")]

    def scores(self, family: str = "", also: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
        return {cid: SEL.score(self.usable(cid, family)) for cid in [*self.pool(), *also]}

    # ------------------------------------------------------------------ one generation

    def pick_parent(self) -> str:
        used: dict[str, int] = {}
        for h in self.state["history"]:
            used[h["parent"]] = used.get(h["parent"], 0) + 1
        return min(self.state["frontier"], key=lambda k: (used.get(k, 0), k))

    def evaluate(self, cid: str, rec: dict[str, Any]) -> bool:
        """The two steps for one new combination; fills ``rec``; False when the evolution has to stop."""
        c = self.state["combos"][cid]
        sel, chk = self.gate["select"], self.gate["check"]
        ts, tc = self.task_label[self.select_family], self.task_label[self.check_family]
        if not self.ensure_runs(cid, self.select_family):
            return False
        rs = self.rewards(cid, self.select_family)
        rec["select_scores"] = [round(x, 4) for x in rs]
        better, why = SEL.better_than_baseline(rs, base=sel["pi0"], margin=sel["margin"], none=sel.get("none"))
        if not better:
            c["status"] = "eliminated"
            rec["outcome"] = f"eliminated: not better than the baseline on {ts}: {why}"
            return True
        first = SEL.select_staged(self.scores(self.select_family, also=(cid,)), task=self.select_family, eps=sel["eps"],
                                  baseline="pi0", cap=self.cap)
        if cid not in first["frontier"]:
            c["status"] = "eliminated"
            rec["outcome"] = f"eliminated on {ts}: " + first["eliminated"].get(cid, "")
            return True
        if not self.ensure_runs(cid, self.check_family):
            return False
        rc = self.rewards(cid, self.check_family)
        rec["check_scores"] = [round(x, 4) for x in rc]
        mean_c = sum(rc) / len(rc)
        if mean_c < chk["floor"] - 1e-9:
            c["status"] = "eliminated"
            rec["outcome"] = f"eliminated: regressed on {tc}: mean {mean_c:.3f} below the floor {chk['floor']:.3f}"
            return True
        c["status"] = "qualified"
        sc = self.scores()
        res = SEL.select_staged(sc, task=self.select_family, eps=sel["eps"], baseline="pi0", cap=self.cap)
        self.state["frontier"] = res["frontier"]
        rec.update(outcome="on the frontier" if cid in res["frontier"] else "eliminated: " + res["eliminated"].get(cid, ""),
                   score={k: sc[cid][k] for k in ("quality", "per_task", "violation_rate", "resources")},
                   baseline={k: sc["pi0"][k] for k in ("quality", "per_task", "violation_rate", "resources")},
                   eliminated=res["eliminated"], noise=res["noise"])
        return True

    def generation(self) -> dict[str, Any]:
        g = self.state["generation"] + 1
        for fam in (self.select_family, self.check_family):
            if not self.ensure_runs("pi0", fam):
                return {"generation": g, "stopped": self.state["stopped"]}
        parent = self.pick_parent()
        gdir = self.out / "generations" / f"g{g:02d}"
        gdir.mkdir(parents=True, exist_ok=True)
        texts = load_skillset(self.skills_dir(parent), forbidden=FORBIDDEN).texts()
        note = build_note(self.usable(parent), self.labels)
        (gdir / "note.md").write_text(note, encoding="utf-8")
        hist = [{k: h.get(k) for k in ("generation", "parent", "changed", "prediction", "select_scores", "check_scores", "outcome")}
                for h in self.state["history"]]
        system = system_prompt(self.task_label[self.select_family], self.task_label[self.check_family], self.gate["select"])
        prop = propose(self.complete, texts, note, hist, system)
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
            self.state["combos"][cid] = {"parent": parent, "generation": g, "runs": {}, "changed": sorted(prop["rewrites"]), "status": "new"}
            rec["combination"] = cid
            self._save()
            if not self.evaluate(cid, rec):
                rec.update(outcome="stopped: " + self.state["stopped"]["why"], stopped=self.state["stopped"])
            rec["flagged_runs"] = [r["run"] for rs in self.state["combos"][cid]["runs"].values() for r in rs if r.get("flagged")]
        rec["frontier_after"] = list(self.state["frontier"])
        changed = sorted(before) != sorted(self.state["frontier"])
        if not self.state["stopped"]:
            self.state["no_change"] = 0 if changed else self.state["no_change"] + 1
        self.state["history"].append(rec)
        self.state["generation"] = g
        self._save()
        (gdir / "generation.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        self.log(json.dumps({"generation": g, "parent": parent, "changed": rec["changed"], "why": " ".join(rec["analysis"].split())[:700],
                             "select_scores": rec.get("select_scores"), "check_scores": rec.get("check_scores"),
                             "outcome": rec["outcome"], "frontier": rec["frontier_after"], "flagged_runs": rec.get("flagged_runs"),
                             "no_change": self.state["no_change"], "tokens_used": self.used_tokens()}, ensure_ascii=False))
        return rec

    def run(self, max_generations: int, should_stop: Callable[[], str] = lambda: "") -> dict[str, Any]:
        self.should_stop = should_stop
        while self.state["generation"] < max_generations and self.state["no_change"] < self.patience and not self.state["stopped"]:
            why = should_stop()
            if why:
                self._stop(why=why)
                break
            self.generation()
        sc = self.scores()
        best = SEL.final_pick(sc, self.state["frontier"], "pi0", self.select_family) if all(k in sc for k in self.state["frontier"]) else None
        self.state["final_pick"] = best
        self._save()
        return {"frontier": self.state["frontier"], "final_pick": best, "generations": self.state["generation"],
                "no_change": self.state["no_change"], "stopped": self.state["stopped"],
                "run_tokens": self.state["run_tokens"], "evolver_tokens": self.state["evolver_tokens"]}
