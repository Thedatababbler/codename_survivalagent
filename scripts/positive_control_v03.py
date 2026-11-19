#!/usr/bin/env python3
"""Positive control of v0.3 (refactor §8): scripted models, the real sandbox, no quota.

    uv run python scripts/positive_control_v03.py

Every check drives the real code path (ledger, planner, executor, sub-agents, the confined environment, the
closing import check) with models that reply fixed commands.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import positive_control as V2  # noqa: E402  (the toy task and helpers of the v0.2 control)
from adamas_unit import importcheck as IC  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402
from survival import memory as MEM  # noqa: E402
from survival.context import ContextWindow, count_messages  # noqa: E402
from survival.ledger import Limits, Refused, RoundLedger  # noqa: E402
from survival.planner import READOUT_RE, Planner, build_state  # noqa: E402
from survival.round import Models, Round, RoundConfig  # noqa: E402
from survival.skills import SKILL_NAMES, SkillError, load_skillset, parse_skill  # noqa: E402

SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
write = V2.write
PI0 = ROOT / "memory" / "planner" / "skills"

MEMORY_OK = "# Memory\n\n## Done\nadd\n\n## Decisions\nnone\n\n## Files changed\ntoyq/__init__.py\n\n## Unfinished\nmul\n\n## Known problems\nnone\n"
ADD = "def add(a, b):\n    return a + b\n"
MUL = ADD + "\ndef mul(a, b):\n    return a * b\n"
PLACEHOLDER = "\n\ndef mul(a, b):\n    raise NotImplementedError\n"
TEST = "from toyq import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"


class Scripted:
    """Replies fixed bash commands, one per call. ``tokens``: prompt tokens booked per call (None: the size of the
    messages, so that the context and the budget move as they would)."""

    def __init__(self, commands: list[str], tokens: int | None = 1000, last: str = SUBMIT):
        self.commands, self.tokens, self.last = list(commands), tokens, last
        self.seen: list[list[dict]] = []

    def query(self, messages, **kw):
        self.seen.append([dict(m) for m in messages])
        cmd = self.commands.pop(0) if self.commands else self.last
        n = self.tokens if self.tokens is not None else count_messages(messages)
        return {"role": "assistant", "content": "", "extra": {"actions": [{"command": cmd}],
                                                             "response": {"usage": {"prompt_tokens": n, "completion_tokens": 10}}}}

    def format_message(self, **kw):
        return dict(kw)

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": f"<returncode>{o['returncode']}</returncode>\n{o['output']}"} for o in outputs]

    def get_template_vars(self, **kw):
        return {}

    def serialize(self):
        return {"info": {"config": {"model": {"model_name": "scripted"}}}}


def factory(scripts: list[Scripted]):
    made: list[Scripted] = []

    def make():
        m = scripts.pop(0) if scripts else Scripted([])
        made.append(m)
        return m
    make.made = made
    return make


def observations(model: Scripted) -> list[str]:
    """Every tool result the model was shown."""
    out, seen = [], set()
    for msgs in model.seen:
        for m in msgs[2:]:
            c = str(m.get("content"))
            if m.get("role") == "user" and c.startswith("<returncode>") and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def main() -> int:
    require_sandbox("positive control v0.3")
    scratch = ROOT / "outputs" / "sandbox"
    scratch.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="pc3-", dir=scratch))
    checks: dict[str, bool] = {}
    try:
        task = V2.synthetic_task(base)
        skills = load_skillset(PI0)
        (task.repo_root / "docs" / "api.md").write_text("# API\n\n```python\nfrom toyq import add, mul\n```\n")
        man = IC.build_manifest(task, out_dir=base / "manifest")
        tdocs = {p.name: p.read_text() for p in IC.manifest_inputs(task)}
        icfg = {k: dict(v) for k, v in IC.DEFAULTS.items()}

        def cfg(**kw) -> RoundConfig:
            lim = {"tokens": 400_000, "seconds": 3600, "compressions": 4, "reviews": 2, "repairs": 2, "workers": 2, "subagents": 4}
            lim.update(kw.pop("limits", {}))
            return RoundConfig(limits=Limits(**lim), probe_every=0, **kw)

        def run(name: str, scripts: list[Scripted], *, arm: str = "pi0", sk=skills, with_ic: bool = True, **kw):
            make = factory(scripts)
            r = Round(task, base / "rounds" / name / "toyq", cfg(**kw), Models(agent=make, summarize=lambda msgs: ("summary", 500, 50)),
                      sk, arm=arm, importcheck=IC.ImportCheck(icfg, man, tdocs) if with_ic else None)
            return r, r.run(), make.made

        # ------------------------------------------------------------------ A: one full pi_0 round
        ex = Scripted([
            write("toyq/__init__.py", ADD),                                         # 1 step
            write("tests/test_add.py", TEST),                                       # 2
            'subagent type=review task="check add against the PRD"',                # refused: no quota
            'subagent type=review budget=0.2 task="check add against the PRD"',     # reviewer
            'subagent type=worker budget=0.2 task="implement mul"',                 # refused: memory not updated
            write("AGENT_MEMORY.md", MEMORY_OK),                                    # free (memory only)
            'subagent type=worker tokens=99999999 task="implement mul"',            # cut to the cap; context replaced
            'request_budget share=0.5 reason="mul needs more"',
            "stage_done",                                                           # stage 1 -> 2
            'subagent type=repair budget=0.1 task="fix nothing"',
            "stage_done",                                                           # ends the round
        ])
        reviewer = Scripted(["echo hacked > toyq/hack.py; echo rc=$?; ls toyq", "echo x >> AGENT_MEMORY.md; echo rc=$?",
                             SUBMIT + "; echo 'toyq/__init__.py: add lacks a docstring'"])
        worker = Scripted([write("toyq/__init__.py", MUL),
                           "printf '\\n## Worker results\\nmul done\\n' >> AGENT_MEMORY.md", SUBMIT + "; echo mul implemented"])
        repairer = Scripted(["true", SUBMIT + "; echo nothing to fix"])
        ra, reca, made = run("A", [ex, reviewer, worker, repairer])
        led = ra.ledger
        ev = led.events
        obs = observations(ex) + observations(reviewer) + observations(worker) + observations(repairer)
        lines = [o.strip().splitlines()[-1] for o in obs]
        checks["1 readout: the last line of every tool result, in the fixed format, for executor and sub-agents"] = (
            len(lines) >= 12 and all(READOUT_RE.match(x) for x in lines))
        checks["3 sub-agent: a call without a quota is refused"] = (
            any(e["event"] == "subagent_refused" and e["why"] == "no quota" for e in ev)
            and any("must name its token quota" in o for o in observations(ex)))
        res = [e for e in ev if e["event"] == "reserve"]
        rel = [e for e in ev if e["event"] == "release"]
        checks["3 sub-agent: a quota over the cap is cut to the cap"] = any(e["cut"] and e["tokens"] == e["cap"] for e in res)
        checks["3 sub-agent: the quota is reserved first and the unused part comes back"] = (
            len(res) == 3 and len(rel) == 3 and all(r["returned"] == q["tokens"] - r["used"] and r["used"] > 0 for q, r in zip(res, rel))
            and all(s["reserved"] == 0 for s in led.snapshot()["stages"].values())
            and led.used == sum(e["prompt_tokens"] for e in ev if e["event"] == "charge"))
        rv = reca["subagents"][0]
        checks["11 reviewer: a write is refused before it happens (read-only policy); nothing changed"] = (
            rv["kind"] == "review" and not rv["code_changed"] and not rv["wrote_memory"] and not (ra.ws / "toyq" / "hack.py").exists()
            and reca["counts"]["reviewer_writes_reverted"] == 0
            and any("rc=1" in o and ("denied" in o.lower() or "read-only" in o.lower()) for o in observations(reviewer)))
        checks["4 handoff: refused while the memory file is not updated after the last code change"] = (
            any(e["event"] == "handoff_refused" for e in ev) and any("handoff refused" in o for o in observations(ex)))
        after = next(msgs for msgs in ex.seen if any("Handoff finished" in str(m.get("content")) for m in msgs))
        checks["4 handoff: afterwards the executor's context is system, task and the memory file"] = (
            len(after) == 3 and "## Worker results" in after[2]["content"] and "mul done" in after[2]["content"]
            and after[0]["role"] == "system" and reca["counts"]["handoffs"] == 1)
        exr = reca["sessions"][0]
        checks["6 management actions are free of steps, their tokens are charged"] = (
            exr["steps"] == 2 and exr["free_turns"] >= 8 and exr["calls"] == 11
            and sum(e["prompt_tokens"] for e in ev if e["event"] == "charge" and e["account"] == "executor") == 11 * 1000)
        tl = ra.token.log
        checks["7 one writer at a time: the token goes executor -> sub-agent -> executor, never two holders"] = (
            [x[1] for x in tl] == ["take"] + ["take", "back"] * 3 and all(tl[i][2] != "executor" for i in (1, 3, 5)))
        try:
            ra.token.take("someone-else")
            from survival.round import OwnedEnvironment
            env = OwnedEnvironment(token=ra.token, owner="executor", workspace=str(ra.ws), role="executor",
                                   capture=str(base / "cap.jsonl"), venv=str(task.venv))
            try:
                env.execute({"command": "true"})
                checks["7 one writer at a time: a session without the token cannot run a command"] = False
            except RuntimeError:
                checks["7 one writer at a time: a session without the token cannot run a command"] = True
            env.close()
        finally:
            ra.token.holder = None
        checks["A the round ends submittable with both stages declared and one top-up granted"] = (
            reca["measures"]["submittable"] == 1 and reca["counts"]["stages_declared"] == 2 and reca["counts"]["topups"] == 1
            and reca["exit_status"] == "Submitted" and not (ra.run_dir / "final" / MEM.MEMORY_FILE).exists())
        sb = breach.scan_run(ra.run_dir, run_id="pc3-A", write=False)
        cap = [json.loads(x) for x in (ra.run_dir / "executor" / "commands.jsonl").read_text().splitlines()]
        checks["sandbox: management commands are in the command capture (the scanner audits every command)"] = (
            sum(1 for c in cap if c.get("management")) >= 6 and any(c["command"].startswith("stage_done") for c in cap))
        import os as _os
        rel_dir = Path(_os.path.relpath(base / "rounds" / "rel" / "toyq"))
        rr = Round(task, rel_dir, cfg(), Models(agent=factory([Scripted([write("toyq/__init__.py", ADD), "stage_done", "stage_done",
                                                                         "stage_done", "stage_done"])])), skills, arm="pi0")
        rr.run()
        checks["sandbox: a relative round directory is made absolute; the agent can write its repository"] = (
            rr.run_dir.is_absolute() and (rr.ws / "toyq" / "__init__.py").is_file())
        checks["A breach scan: every command of every session captured, no CRITICAL"] = (
            sb["critical"] == 0 and sb["commands"] >= 9)

        # ------------------------------------------------------------------ the agent user (2026-11-19)
        import stat as _stat
        from adamas_unit.sandbox_env import WorkspaceAccessError
        from orchestra.sandbox import policy as _SB
        from survival.round import OwnedEnvironment
        root_mode = _stat.S_IMODE(_os.stat("/root").st_mode)      # chmod to the mode it already has: a no-op if it worked
        pu = Scripted([write("run.sh", "echo hi"),
                       "chmod +x run.sh; echo own_rc=$?; test -x run.sh && echo own_x=yes; id -u",
                       f"chmod {root_mode:o} /root; echo root_rc=$?",
                       "python -c \"import os; os.chmod('/root/projects', 0o775)\"; echo py_rc=$?",
                       write("toyq/__init__.py", ADD), "stage_done", "stage_done", "stage_done", "stage_done"])
        ru = Round(task, base / "rounds" / "uid" / "toyq", cfg(), Models(agent=factory([pu])), skills, arm="pi0")
        ru.run()
        ou = "\n".join(observations(pu))
        su = breach.scan_run(ru.run_dir, run_id="pc3-uid", write=False)
        pc_events = [e for e in su["events"] if e["event"] == "permission_change"] if "events" in su else None
        if pc_events is None:
            cu = breach.calls(ru.run_dir)
            wr = breach._writable_roots(ru.run_dir)
            pc_events = [c for c in cu if breach.permission_change_target(c["command"], wr)]
        checks["agent user: commands run as the ordinary user, not root"] = f"\n{_SB.agent_uid()}\n" in ou + "\n"
        checks["agent user: chmod +x in its own repository succeeds and is not flagged"] = (
            "own_rc=0" in ou and "own_x=yes" in ou and not any("run.sh" in str(e.get("command")) for e in pc_events))
        checks["agent user: chmod /root fails (shell and Python) and /root is unchanged"] = (
            "root_rc=1" in ou and "py_rc=1" in ou and _stat.S_IMODE(_os.stat("/root").st_mode) == root_mode)
        checks["scanner: chmod on a path outside the repository is CRITICAL (the run is void)"] = (
            len(pc_events) == 2 and su["critical"] >= 2 and su["void"])
        wr0 = ["/x/run/ws", "/tmp/unit-a"]
        pct = breach.permission_change_target
        checks["scanner: permission_change cases (loop variable, cd outside, .., setfacl, Python; repository paths pass)"] = (
            bool(pct("chmod 777 /root /root/projects /x/run/ws 2>&1; echo hi > a", wr0))
            and bool(pct('for p in /x/run/ws /x/run; do chmod 777 "$p"; done', wr0))
            and bool(pct("cd /x/run && chown root ws", wr0)) and bool(pct("chgrp staff ../other", wr0))
            and bool(pct("setfacl -m u:1:rwx /etc", wr0))
            and bool(pct("python - <<'EOF'\nimport os\nfor p in ['/x/run/ws', '/root']:\n os.chmod(p, 0o777)\nEOF", wr0))
            and not pct("chmod +x run.sh scripts/a.sh; chmod -R u+rwX .; chmod 755 /x/run/ws/bin/tool", wr0)
            and not pct("python -c \"import os; os.chmod('run.sh', 0o755)\"; ls /usr/bin", wr0)
            and not pct('for f in scripts/*; do chmod +x "$f"; done', wr0))

        # the start-up check: the program, not the agent, finds a repository the agent cannot use
        pf = base / "rounds" / "preflight"
        (pf / "ws").mkdir(parents=True)
        from survival.agents import WriteToken as _WT
        e_ok = OwnedEnvironment(token=_WT(), owner="x", workspace=str(pf / "ws"), role="executor", capture=str(pf / "c.jsonl"),
                                venv=str(task.venv))
        e_ro = OwnedEnvironment(token=_WT(), owner="x", read_only=True, workspace=str(pf / "ws"), role="review",
                                capture=str(pf / "r.jsonl"), venv=str(task.venv))
        refused = {}
        for label, kw in (("unreadable", {"writable": True}), ("writable_for_reviewer", {"writable": False})):
            env_x = e_ro if label == "unreadable" else e_ok
            if label == "unreadable":                 # a policy without the repository: what forced-1 had
                from dataclasses import replace as _rep
                pol = env_x.policy
                env_x.policy_path = _SB.write_policy(_rep(pol, read=[r for r in pol.read if r != str(pf / "ws")]),
                                                     env_x.private / "policy_x.json")
            try:
                env_x.preflight(**kw)
                refused[label] = False
            except WorkspaceAccessError:
                refused[label] = True
        e_ok.close()
        e_ro.close()
        checks["start-up check: a repository the agent cannot read stops the run before any agent command"] = refused["unreadable"]
        checks["start-up check: a reviewer whose repository is writable is refused"] = refused["writable_for_reviewer"]
        from survival.round import role_text as _rt
        from adamas_unit.sessions import role_prompt as _rp
        checks["prompts: every agent is told to stop and report, and not to change permissions"] = all(
            "Do not try to change any permissions" in t for t in (_rt("common"), _rp("importfix"), _rp("builder")))

        # ------------------------------------------------------------------ 3: call limits
        exl = Scripted(['subagent type=repair budget=0.05 task="a"', 'subagent type=repair budget=0.05 task="b"',
                        'subagent type=repair budget=0.05 task="c"', 'subagent type=review budget=0.05 task="d"',
                        'subagent type=review budget=0.05 task="e"',
                        "stage_done", "stage_done", "stage_done"])
        rl, recl, _ = run("limits", [exl] + [Scripted([]) for _ in range(5)], with_ic=False,
                          limits={"repairs": 2, "reviews": 2, "workers": 2, "subagents": 3})
        why = [e["why"] for e in rl.ledger.events if e["event"] == "subagent_refused"]
        checks["3 sub-agent: the limit of a kind and the limit of all calls are enforced"] = (
            why == ["kind limit", "total limit"] and recl["counts"]["subagents"] == 3
            and rl.ledger.calls == {"review": 1, "repair": 2, "worker": 0})

        # ------------------------------------------------------------------ 12: the stage minimum
        exm = Scripted([write("toyq/__init__.py", "def add(a, b:\n"), "stage_done", "stage_done", "stage_done"])
        rm, recm, _ = run("minimum", [exm], with_ic=False)
        checks["12 stage minimum: the first declaration is refused with the reason, the second passes flagged"] = (
            any("not accepted yet" in o and "import toyq" in o for o in observations(exm))
            and recm["stages"][0]["declared_done"] and recm["stages"][0]["below_minimum"]
            and recm["counts"]["stages_below_minimum"] >= 1)

        # ------------------------------------------------------------------ 2: the context window
        big = "python -c \"print('x ' * 6000)\""
        exw = Scripted([big, "true", "true", "true", "true", "true"], tokens=None, last="stage_done")
        rw, recw, _ = run("window", [exw], with_ic=False, context_window=4000, max_rollbacks=2)
        evw = rw.ledger.events
        rb = [e for e in evw if e["event"] == "rollback"]
        charged = [e["prompt_tokens"] for e in evw if e["event"] == "charge"]
        checks["2 context: a turn over the window is undone, a note says to compact, the retries are capped"] = (
            1 <= len(rb) <= 2 and all(e["after"] < e["before"] for e in rb)
            and any("made the context larger than the window" in str(m.get("content")) for msgs in exw.seen for m in msgs)
            and recw["counts"]["rollbacks"] == len(rb))
        checks["2 context: tokens already spent are not returned by a roll-back"] = (
            rw.ledger.used == sum(charged) and all(a <= b for a, b in zip(
                [sum(charged[:i]) for i in range(len(charged))], [sum(charged[:i + 1]) for i in range(len(charged))])))

        # a management command as the last line of a script; the forced compaction when few turns are left
        ext = Scripted([write("toyq/__init__.py", ADD), "true", "true", "true", "true", "true", "true", "true",
                        write("AGENT_MEMORY.md", MEMORY_OK) + "\ncompact", "true", "stage_done", "stage_done", "stage_done", "stage_done"])
        rt, rect, _ = run("trailing", [ext], with_ic=False)
        checks["management: `compact` as the last line of a script runs the script, then compacts"] = (
            rect["resources"]["compressions"] == 1 and not any("command not found" in o for o in observations(ext))
            and any("Context compacted" in str(m.get("content")) for msgs in ext.seen for m in msgs))
        exf = Scripted([big, big, big, big, big, big], tokens=None, last="stage_done")
        rf, recf, _ = run("forced", [exf], with_ic=False, context_window=4000, max_rollbacks=1)
        checks["2 context: past the retry cap an oversized turn is still undone; the session does not end on it"] = (
            recf["counts"]["rollbacks"] >= 3 and recf["exit_status"] != "ContextExhausted")

        exl = Scripted([big] * 12, tokens=None, last="stage_done")
        rl, recl, _ = run("rblimit", [exl], with_ic=False, context_window=4000, max_rollbacks=1, rollback_limit=4)
        checks["2 context: a session that keeps overflowing ends at the roll-back limit, and the diagnosis says so"] = (
            recl["exit_status"] == "RollbackLimit" and recl["counts"]["rollbacks"] == 4
            and recl["counts"]["rollback_limit_sessions"] == 1
            and any(e["event"] == "rollback_limit" for e in rl.ledger.events))

        # ------------------------------------------------------------------ 10: pi_0's triggers on a simulated run
        ledp = RoundLedger(Limits(tokens=100_000), 2, path=base / "p0.json")
        ledp.open_stage(0)
        pl = Planner(skills, ledp)
        fired = []
        for used, ctx in [(10_000, 0.2), (30_000, 0.5), (44_000, 0.6), (45_500, 0.7), (46_000, 0.76), (47_000, 0.8), (47_500, 0.3)]:
            ledp.charge(used - ledp.used)
            st = build_state(ledp, context_share=ctx, idle_steps=0, missing_delta=0, tests_delta=0, all_done=False)
            fired.append([(a.skill, a.action) for a in pl.tick(st)])
        got, _txt = pl.approve(10_000_000, "more", build_state(ledp, context_share=0.3, idle_steps=0, missing_delta=0,
                                                              tests_delta=0, all_done=False))
        checks["10 pi_0: one reminder at 0.90 of the stage, framework compaction at 0.75 of the window, no suggestions"] = (
            fired == [[], [], [], [("token-reminder", "remind")], [("memory-compaction", "framework_compaction")], [], []])
        checks["10 pi_0: a top-up is approved up to the mechanical cap, never beyond (the later stage keeps its floor)"] = (
            got == ledp.events[-1]["cap"] and ledp.left() - (ledp.stage().allocation - ledp.stage().used) >= ledp.floor())
        judged = (PI0 / "budget-approval" / "SKILL.md").read_text().replace("mode: approve_within_cap", "mode: judge")
        from survival.skills import SkillSet
        skj = SkillSet({**skills.skills, "budget-approval": parse_skill(judged)})
        ledj = RoundLedger(Limits(tokens=100_000), 2, path=base / "pj.json")
        ledj.open_stage(0)
        plj = Planner(skj, ledj, complete=lambda msgs: ('{"share": 7.5, "reason": "all of it and more"}', 321, 9))
        capj = ledj.grant_cap()
        gotj, _t = plj.approve(10_000_000, "more", build_state(ledj, context_share=0.1, idle_steps=0, missing_delta=0, tests_delta=0, all_done=False))
        checks["4.4 judgment: one planner call, its answer cut to the cap, its tokens on the management account only"] = (
            gotj == capj and ledj.side["management"]["tokens"] == 321 and ledj.used == 0
            and plj.decisions[-1]["decided_by"] == "planner")
        exc = Scripted(["true"] * 8, tokens=None, last="stage_done")
        exc.commands.insert(0, "python -c \"print('y ' * 1200)\"")
        rc_, recc, _ = run("compaction", [exc], with_ic=False, context_window=4000, keep_recent=1)
        checks["10 pi_0: the framework compacts (a summary, no memory file needed) and the pool is counted"] = (
            recc["resources"]["compressions"] >= 1
            and any(e["event"] == "compaction_done" and not e["from_memory"] for e in rc_.ledger.events)
            and any("summary of your earlier work" in str(m.get("content")) for msgs in exc.seen for m in msgs))

        # ------------------------------------------------------------------ 5: skill validation
        good = (PI0 / "memory-compaction" / "SKILL.md").read_text()

        def rejected(text: str, needle: str, **kw) -> bool:
            try:
                parse_skill(text, **kw)
                return False
            except SkillError as e:
                return any(needle in p for p in e.problems)
        checks["5 skill: a condition on something outside the planner state is refused"] = rejected(
            good.replace("context >= 0.75", "files_changed >= 3"), "not part of the planner state")
        checks["5 skill: an absolute token number is refused (header and body)"] = (
            rejected(good.replace("context >= 0.75", "context >= 48000"), "not a count")
            and rejected(good.replace("## What to do\n", "## What to do\nCompact when you pass 48000 tokens.\n"), "absolute amount"))
        checks["5 skill: a task, package or function name is refused"] = (
            rejected(good.replace("## What to do\n", "## What to do\nIn toyq keep the notes on add_numbers.\n"), "names 'toyq'",
                     forbidden={"toyq", "add_numbers"})
            and rejected(good.replace("## What to do\n", "## What to do\nRe-read helpers.py first.\n"), "reads as a file"))
        checks["5 skill: the six pi_0 documents pass, each with the four body sections"] = (
            set(skills.skills) == set(SKILL_NAMES) and all(len(s.sections) >= 4 for s in skills.skills.values()))

        # ------------------------------------------------------------------ 8: the closing import check, every arm
        def ic_round(name: str, arm: str):
            exq = Scripted([write("toyq/__init__.py", ADD), "stage_done", "stage_done"], last="true")
            fixq = Scripted([write("toyq/__init__.py", ADD + PLACEHOLDER), SUBMIT])
            return run(name, [exq, fixq], arm=arm, executor_max_steps=4)
        rp, recp, _ = ic_round("ic_pi0", "pi0")
        rs, recs, _ = ic_round("ic_single", "single")
        fp, fs = recp["importcheck"], recs["importcheck"]
        checks["8 import check and its one fix: the same for every arm, on their own ledger, not on the round's"] = (
            fp["fixes_triggered"] == fs["fixes_triggered"] == 1 and fp["fixes_kept"] == fs["fixes_kept"] == 1
            and fp["fix_tokens"] == fs["fix_tokens"] == 2000 and fp["manifest_sha256"] == fs["manifest_sha256"] == man["sha256"]
            and fp["config"] == fs["config"]
            and all(r.ledger.used == sum(e["prompt_tokens"] for e in r.ledger.events if e["event"] == "charge") for r in (rp, rs))
            and recp["resources"]["tokens"] == rp.ledger.used and "importfix" not in json.dumps(rp.ledger.events))
        checks["single arm: no sub-agents, and it keeps working after the last stage is declared"] = (
            recs["counts"]["subagents"] == 0 and recs["sessions"][0]["calls"] > 3 and recs["exit_status"] in ("StepLimit", "CallCap")
            and any("keep improving" in o for o in observations(_[0])))
        exn = Scripted(['subagent type=review budget=0.2 task="x"', "stage_done", "stage_done"], last="true")
        rn, recn, _m = run("single_refuse", [exn], arm="single", with_ic=False, executor_max_steps=3)
        checks["single arm: a sub-agent call is refused"] = any("not available in this run" in o for o in observations(exn))
        la, lb = [observations(m)[0].strip().splitlines()[-1] for m in (made[0], _[0])]
        checks["1 readout: the same format in the pi_0 arm and the single arm"] = bool(READOUT_RE.match(la) and READOUT_RE.match(lb))

        # ------------------------------------------------------------------ 11b: the reviewer's second line of defence
        exg = Scripted([write("toyq/__init__.py", ADD), 'subagent type=review budget=0.2 task="check"', "stage_done", "stage_done"])
        rvg = Scripted(["echo hacked > toyq/hack.py", SUBMIT + "; echo done"])
        make = factory([exg, rvg])
        rg = Round(task, base / "rounds" / "guard" / "toyq", cfg(), Models(agent=make), skills, arm="pi0")
        rg.review_read_only = False
        recg = rg.run()
        checks["11 reviewer: if a write got through, the program undoes it and flags it (second line of defence)"] = (
            recg["counts"]["reviewer_writes_reverted"] == 1 and not (rg.ws / "toyq" / "hack.py").exists()
            and (rg.ws / "toyq" / "__init__.py").read_text().strip() == ADD.strip()
            and any(e["event"] == "reviewer_wrote" for e in rg.ledger.events))

        # ------------------------------------------------------------------ 9: what the evolver reads
        from survival import frontier as FR
        from survival.evolve import SYSTEM as EV_SYSTEM, Evolution
        from survival.note import digest

        dg = digest(reca, 0.5)
        blob = json.dumps(dg)
        calls: list[list[dict]] = []
        good_mc = (PI0 / "memory-compaction" / "SKILL.md").read_text().replace("context >= 0.75", "context >= 0.60")
        bad_mc = good_mc.replace("context >= 0.60", "context >= 60000")
        answers = [f"<<<ANALYSIS>>>\nT1-R2 compacted late\n<<<END>>>\n<<<PREDICTION>>>\n{{\"quality\": \"same\"}}\n<<<END>>>\n<<<SKILL name=memory-compaction>>>\n{t}\n<<<END>>>"
                   for t in (bad_mc, good_mc)]
        answers += [answers[1].replace("0.60", f"0.{60 + i}") for i in range(1, 9)]

        def fake_complete(msgs):
            calls.append(msgs)
            return answers.pop(0), 1000, 100
        table = {"pi0": (0.30, 100.0), "g01": (0.31, 80.0), "g02": (0.20, 50.0), "g03": (0.305, 120.0)}

        def fake_runner(cid, sdir, t, rep):
            q, tok = table.get(cid, (0.30, 100.0))
            d = json.loads(blob)
            d["hidden_pass_ratio"] = q + 0.002 * rep
            return {"round": f"{cid}-{t}-{rep}", "usable": True, "ratio": q + 0.002 * rep, "submittable": 1,
                    "resources": {"tokens": tok, "repairs": 1, "compressions": 1, "peak_context": 10.0}, "digest": d}
        evo = Evolution(base / "evo", tasks=["toyq", "toyq2"], runner=fake_runner, complete=fake_complete,
                        eps={"toyq": 0.02, "toyq2": 0.02}, base_skills=PI0, forbidden={"toyq", "add_numbers"},
                        leak_check=lambda t: True, patience=2, log=lambda s: None)
        g1 = evo.generation()
        sent = "\n".join(str(m["content"]) for m in calls[0])
        checks["9 evolver input: figures only -- no code, no test, no command, no file or task name"] = (
            not any(w in sent for w in ("def add", "toyq", "test_add", "__init__", "import ", "COMPLETE_TASK", "::", "hack.py", "u0_add"))
            and "hidden_pass_ratio" in sent and "T1-R1" in sent and "worse" in sent and "better" in sent)
        checks["evolution: an invalid document goes back once with its problems; the corrected one is run"] = (
            len(calls) == 2 and "not a count" in calls[1][-1]["content"] and g1["changed"] == ["memory-compaction"]
            and g1["tries"][0]["problems"] and not g1["tries"][1]["problems"]
            and "context >= 0.60" in (evo.skills_dir("g01") / "memory-compaction" / "SKILL.md").read_text()
            and (evo.skills_dir("g01") / "token-reminder" / "SKILL.md").read_text() == (PI0 / "token-reminder" / "SKILL.md").read_text().strip() + "\n")
        checks["evolution: same quality within the noise and fewer tokens -> it replaces pi_0 on the frontier"] = (
            g1["frontier_after"] == ["g01"] and "dominated" in g1["eliminated"]["pi0"])
        g2 = evo.generation()
        g3 = evo.generation()
        checks["evolution: clearly lower quality is eliminated whatever it saves; dominated on resources is eliminated"] = (
            g2["outcome"].startswith("eliminated: quality") and g3["outcome"].startswith("eliminated: same quality")
            and evo.state["frontier"] == ["g01"] and evo.state["no_change"] == 2)
        res = evo.run(max_generations=10)
        checks["evolution: stops after the set number of proposals that leave the frontier unchanged; picks from the frontier"] = (
            res["generations"] == 3 and res["final_pick"] == "g01" and res["evolver_tokens"]["prompt"] == 4000)
        many = {f"c{i}": {"quality": 0.3, "per_task": {"a": 0.3}, "submittable": 1.0,
                          "resources": {"tokens": 100 + i, "repairs": 10 - i, "compressions": 1, "peak_context": 5}} for i in range(10)}
        many["pi0"] = {"quality": 0.3, "per_task": {"a": 0.3}, "submittable": 1.0,
                       "resources": {"tokens": 200, "repairs": 20, "compressions": 2, "peak_context": 9}}
        many["unsub"] = {"quality": 0.9, "per_task": {"a": 0.9}, "submittable": 0.5,
                         "resources": {"tokens": 1, "repairs": 0, "compressions": 0, "peak_context": 1}}
        selm = FR.select(many, eps={"a": 0.01}, baseline="pi0", cap=8)
        checks["frontier: less submittable than pi_0 is out first; at most 8 stay"] = (
            len(selm["frontier"]) == 8 and "unsub" not in selm["frontier"] and "submittable" in selm["eliminated"]["unsub"])
        checks["evolver prompt: rewrites at most 2 documents; names no task"] = (
            "AT MOST 2" in EV_SYSTEM and not any(w in EV_SYSTEM.lower() for w in ("deepdiff", "pathspec", "fsutil", "toyq")))
        # ---- a reply without a tool call gets a hint, the session goes on (user decision 2026-11-19)
        from minisweagent.exceptions import FormatError as _FE

        class NoCall(Scripted):
            def query(self, messages, **kw):
                if self.commands and self.commands[0] == "NOCALL":
                    self.commands.pop(0)
                    self.seen.append([dict(m) for m in messages])
                    raise _FE({"role": "user", "content": "No tool calls found in the response. Every response MUST include at least one tool call.",
                               "extra": {"response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 10}}}})
                return super().query(messages, **kw)

        exn = NoCall([write("toyq/__init__.py", MUL), "stage_done", "NOCALL", "NOCALL", "NOCALL", "NOCALL", "stage_done"])
        rn, recn, _ = run("nocall", [exn], with_ic=False)
        hints = [str(m.get("content")) for m in exn.seen[-1] if "If you want to submit, use the submit command" in str(m.get("content"))]
        checks["2 no tool call: the executor is told how to submit or to call a tool, four times in a row, and the session goes on to submit"] = (
            recn["exit_status"] == "Submitted" and len(hints) == 4 and all("`stage_done`" in h and "call a tool" in h for h in hints)
            and recn["sessions"][0]["format_hints"] == 4 and recn["sessions"][0]["calls"] == 7
            and sum(e["prompt_tokens"] for e in rn.ledger.events if e["event"] == "charge") == 7000)
        exn2 = NoCall(["NOCALL"] * 12)
        rn2, recn2, _ = run("nocall-forever", [exn2], with_ic=False)
        checks["2 no tool call: a session that never calls a tool still ends (ten in a row), every call charged"] = (
            recn2["exit_status"] == "RepeatedFormatError" and recn2["sessions"][0]["calls"] == 10
            and sum(e["prompt_tokens"] for e in rn2.ledger.events if e["event"] == "charge") == 10_000)

        # ---- completion-check (the sixth skill, user decision 2026-11-19)
        cc_text = (PI0 / "completion-check" / "SKILL.md").read_text()

        def cc_skills(name: str, when: str, action: str, **params):
            d = base / "skills" / name
            shutil.copytree(PI0, d)
            t = cc_text.replace('when: "true"', f'when: "{when}"').replace("action: accept", f"action: {action}")
            for k, v in params.items():
                t = re.sub(rf"(?m)^  {k}: .*$", f"  {k}: {v}", t)
            (d / "completion-check" / "SKILL.md").write_text(t)
            return load_skillset(d)

        import re
        checks["5 completion-check: pi_0 accepts every declaration (no hold, nothing recorded, the round as before)"] = (
            not any(f["skill"] == "completion-check" for f in reca["planner"]["fired"])
            and not any(e["event"] == "completion_check" for e in ev) and reca["exit_status"] == "Submitted")
        checks["5 completion-check: an unknown action, a state outside the planner's, an absolute amount are refused"] = (
            rejected(cc_text.replace("action: accept", "action: run_hidden_tests"), "is not an action of completion-check")
            and rejected(cc_text.replace('when: "true"', 'when: "hidden_ratio < 0.5"'), "not part of the planner state")
            and rejected(cc_text.replace('when: "true"', 'when: "idle_steps > 5000"'), "is not a count")
            and rejected(cc_text.replace("max_prompts: 3", "max_prompts: 5000"), "is not a count"))
        calib = load_skillset(ROOT / "configs" / "v03_calibration_skills")
        exc = Scripted([write("toyq/__init__.py", ADD), "stage_done",      # stage 1: accepted (not the last stage)
                        "stage_done",                                       # last stage: prompt 1
                        write("toyq/__init__.py", MUL), "stage_done",      # code changed -> prompt 2
                        write("AGENT_MEMORY.md", MEMORY_OK), "stage_done",  # only the memory file -> idle 1, prompt 3
                        "stage_done"])                                      # idle 2 -> accepted, submitted
        rc_, recc, _ = run("continue", [exc], sk=calib, with_ic=False)
        held = [o for o in observations(exc) if "not accepted yet (completion check, prompt" in o]
        ccs = [f for f in recc["planner"]["fired"] if f["skill"] == "completion-check"]
        checks["5 completion-check prompt_continue: the last declaration is held with the usage so far, until two prompts in a row changed no code"] = (
            recc["exit_status"] == "Submitted" and len(held) == 3 and all("Used so far:" in h and "% of this stage's tokens" in h for h in held)
            and [f["outcome"] for f in ccs] == ["held", "held", "held", "accepted (2 prompts in a row changed no code)"]
            and recc["stages"][0]["declared_done"] and recc["sessions"][0]["calls"] == 8
            and "def mul" in (rc_.run_dir / "final" / "toyq" / "__init__.py").read_text())
        exr2 = Scripted([write("toyq/__init__.py", MUL), "stage_done",
                         'subagent type=review budget=0.2 task="check the stage"', "stage_done", "stage_done", "stage_done"])
        rr2, recr2, _ = run("req-review", [exr2, Scripted([SUBMIT + "; echo fine"])], sk=cc_skills("req-review", "reviews < 1", "require_review"), with_ic=False)
        o2 = observations(exr2)
        checks["5 completion-check require_review: held until a review was made in the stage, then accepted"] = (
            recr2["exit_status"] == "Submitted" and sum("Have this stage's work reviewed first" in o for o in o2) == 1
            and recr2["counts"]["reviews"] == 1 and recr2["sessions"][0]["calls"] == 5)
        TEST_MUL = "from toyq import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
        exs = Scripted([write("toyq/__init__.py", MUL), "stage_done", "stage_done",
                        write("tests/test_add.py", TEST), "stage_done",        # one test passes -> stage 1 accepted
                        "stage_done",                                           # stage 2: no new passing test -> held
                        write("tests/test_mul.py", TEST_MUL), "stage_done"])
        rs2, recs2, _ = run("req-selftest", [exs], sk=cc_skills("req-selftest", "stage_progress >= 0.0", "require_selftest", max_refusals=5), with_ic=False)
        checks["5 completion-check require_selftest: held until more of the executor's own tests pass than when the stage began"] = (
            recs2["exit_status"] == "Submitted" and sum("No test of your own for this stage's work passes" in o for o in observations(exs)) == 3
            and recs2["sessions"][0]["calls"] == 8)
        exd = Scripted([write("toyq/__init__.py", MUL), "stage_done", "stage_done", "stage_done", "stage_done"])
        rd2, recd2, _ = run("req-drop", [exd], sk=cc_skills("req-drop", "stage_progress >= 0.0", "require_selftest", max_refusals=1), with_ic=False)
        checks["5 completion-check: a requirement is dropped after max_refusals (a stage is never held for ever)"] = (
            recd2["exit_status"] == "Submitted" and sum("No test of your own for this stage's work passes" in o for o in observations(exd)) == 2
            and any("refused 1 times" in f["outcome"] for f in recd2["planner"]["fired"]))

        probe_sk = load_skillset(ROOT / "tests" / "v03_probe_skills")
        DOC = '"""toyq."""\n'
        exp = Scripted([write("toyq/__init__.py", ADD), write("tests/test_add.py", TEST),
                        "stage_done",                                                   # tests pass -> review asked
                        'subagent type=review budget=0.2 task="check stage 1"',
                        "stage_done",                                                   # reviewed, code unchanged -> held
                        write("toyq/__init__.py", MUL), "stage_done",                  # changed after the review -> stage 2
                        "stage_done",                                                   # no new passing test -> held
                        write("tests/test_mul.py", TEST_MUL), "stage_done",            # -> review asked
                        'subagent type=review budget=0.2 task="check stage 2"',
                        "stage_done",                                                   # unchanged -> held
                        write("toyq/__init__.py", DOC + MUL), "stage_done"])           # -> submitted
        rp, recp, _ = run("probe", [exp, Scripted([SUBMIT + "; echo add a docstring"]), Scripted([SUBMIT + "; echo add a docstring"])],
                          sk=probe_sk, with_ic=False)
        op = observations(exp)
        outs = [(f["action"], f["outcome"].split(" (")[0]) for f in recp["planner"]["fired"] if f["skill"] == "completion-check"]
        checks["5 completion-check chain (probe skills): each stage is held for own tests, then a review, then a change after the review, then accepted"] = (
            recp["exit_status"] == "Submitted" and recp["counts"]["reviews"] == 2 and recp["counts"]["stages_declared"] == 2
            and sum("No test of your own for this stage's work passes" in o for o in op) == 1
            and sum("Have this stage's work reviewed first" in o for o in op) == 2
            and sum("The code is unchanged since the review" in o for o in op) == 2
            and outs == [("require_selftest", "accepted"), ("require_review", "held"),
                         ("require_selftest", "accepted"), ("require_review", "held"),
                         ("require_selftest", "accepted"), ("require_review", "accepted"),
                         ("require_selftest", "held"),
                         ("require_selftest", "accepted"), ("require_review", "held"),
                         ("require_selftest", "accepted"), ("require_review", "held"),
                         ("require_selftest", "accepted"), ("require_review", "accepted")])

        exl = Scripted([write("toyq/__init__.py", MUL), "stage_done", "stage_done"])
        rl, recl, _ = run("last-declaration", [exl], with_ic=False)
        ran_l = breach._trajectory_commands(rl.run_dir / "executor" / "trajectory.json")
        got_l = len(breach._captured(rl.run_dir / "executor" / "commands.jsonl"))
        scan_l = breach.scan_run(rl.run_dir, run_id="pc-last-declaration", write=False)
        checks["2 capture: the declaration that ends the session is captured too (every command of the trajectory is in the capture)"] = (
            recl["exit_status"] == "Submitted" and ran_l == got_l == 3 and scan_l["critical"] == 0)

        # ---- sealed scoring is isolated (user decision 2026-11-19): an ordinary user, file rules on, attempts recorded
        import os as _os
        import stat as _stat
        from orchestra.sandbox import policy as _SB
        from orchestra.sandbox.guard import run_confined as _rc
        (ROOT / "outputs" / "sandbox").mkdir(parents=True, exist_ok=True)
        sc = Path(tempfile.mkdtemp(prefix="pc-score-", dir=ROOT / "outputs" / "sandbox")) / "repo"
        sc.mkdir()
        other = ROOT / "outputs" / "sandbox" / (sc.parent.name + "-other")
        other.mkdir()
        (other / "secret.txt").write_text("not yours\n")
        (sc / "own.txt").write_text("mine\n")
        (sc / "probe.py").write_text("""
import json, os, subprocess
out = {"uid": os.getuid(), "euid": os.geteuid()}
def t(name, fn):
    try:
        fn(); out[name] = "ok"
    except Exception as e:
        out[name] = type(e).__name__
t("chmod_root", lambda: os.chmod("/root", 0o777))
out["chmod_prog"] = subprocess.run(["chmod", "777", "/root"], capture_output=True).returncode
t("read_config", lambda: open(%r).read())
t("read_other", lambda: open(%r).read())
t("list_other", lambda: os.listdir(%r))
t("list_root", lambda: os.listdir("/root"))
t("read_own", lambda: open("own.txt").read())
t("chmod_own", lambda: os.chmod("own.txt", 0o600))
t("write_own", lambda: open("new.txt", "w").write("x"))
print(json.dumps(out))
""" % (str(ROOT / "configs" / "sandbox.yaml"), str(other / "secret.txt"), str(other)))
        root_mode = _stat.S_IMODE(_os.stat("/root").st_mode)
        try:
            pr = _rc(["/usr/bin/python3", "probe.py"], cwd=sc, mode="scoring", role="pc_scoring", capture_output=True, text=True,
                     timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": str(sc)})
            got = json.loads(pr.stdout.strip().splitlines()[-1])
            ev = getattr(pr, "scoring_audit", [])
        except Exception as e:  # noqa: BLE001
            got, ev = {"error": repr(e), "out": getattr(locals().get("pr"), "stderr", "")}, []
            print("scoring probe failed:", got)
        finally:
            shutil.rmtree(sc.parent, ignore_errors=True)
            shutil.rmtree(other, ignore_errors=True)
        perm = [e for e in ev if e["event"] == "permission_change" and e["target"] == "/root"]
        checks["scoring: the scored code runs as the scoring user (not root, not the agent user)"] = (
            got.get("uid") == got.get("euid") == _SB.scoring_uid() and _SB.scoring_uid() not in (0, _SB.agent_uid()))
        checks["scoring: chmod /root by the scored code fails (call and program), /root keeps its mode, both recorded CRITICAL"] = (
            got.get("chmod_root") == "PermissionError" and got.get("chmod_prog") not in (0, None)
            and _stat.S_IMODE(_os.stat("/root").st_mode) == root_mode
            and {e["how"] for e in perm} >= {"os.chmod", "subprocess.Popen:chmod"} and all(e["level"] == "CRITICAL" for e in perm))
        checks["scoring: reading or listing any other directory fails and is recorded"] = (
            got.get("read_config") == "PermissionError" and got.get("read_other") == "PermissionError"
            and got.get("list_other") == "PermissionError" and got.get("list_root") == "PermissionError"
            and {e["target"] for e in ev if e["event"] == "read_denied"} >= {str(ROOT / "configs" / "sandbox.yaml"), str(other / "secret.txt")}
            and {e["target"] for e in ev if e["event"] == "list_denied"} >= {str(other), "/root"})
        checks["scoring: its own copy stays readable and writable; nothing of that is recorded"] = (
            got.get("read_own") == "ok" and got.get("chmod_own") == "ok" and got.get("write_own") == "ok"
            and not any(str(sc) in e["target"] for e in ev))
        try:
            _SB.confine_scoring(["/usr/bin/python3", "-c", "pass"], {"PATH": "/usr/bin:/bin"})
            refused = False
        except _SB.SandboxRequiredError:
            refused = True
        checks["scoring: refuses to run without a scratch copy to confine it to (no open-file scoring any more)"] = refused
    finally:
        for p in sorted(base.rglob("*"), reverse=True):
            try:
                p.chmod(0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        shutil.rmtree(base, ignore_errors=True)
    for k, v in sorted(checks.items(), key=lambda kv: (len(kv[0].split()[0]), kv[0])):
        print(("ok  " if v else "FAIL"), k)
    ok_all = all(checks.values()) and len(checks) >= 20
    print(json.dumps({"ok": ok_all, "passed": sum(checks.values()), "checks": len(checks)}))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
