#!/usr/bin/env python3
"""Positive control of the AdaMAS-unit pipeline (docs/USING_ADAMAS.md §4 item 13). No model call, no quota.

A two-unit synthetic task (package ``toyq``: unit 0 ``add``, unit 1 ``mul``) runs through the real Round: real
sandbox, real gate, ledger, planner (fixed rule), unit runner, submittable check and breach scanner; a scripted model
replaces the agents and makes fixed edits:

  unit 0  build: add with two planted bugs (negative, float) + a top-up request far above the cap
          repair 1 fixes "negative"; repairs 2-3 change nothing -> continue mode with "float" failing
  unit 1  build: mul, and breaks add for positive numbers (a regression); repair 1 fixes it ->
          own gate passes, "float" stays inherited; the round's repair cap (2 x 2 = 4) leaves no repair for it

Checks: the gate finds the planted bugs; repair passes the gate; columns 1 / 2 / 3; continue mode; tokens booked and
carried over; the grant cut to the mechanical cap; the round's repair cap; the builder cannot read the frozen suites
and the repairer can read its evidence; the submittable check tells importable from broken code; an exhausted
round only gates; the evolver's mechanical check rejects a too-large move and a sum over B; a changed frozen suite
stops a round; every command captured; a planner-state canary in a workspace voids the run.

    uv run python scripts/positive_control.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit import submit_check  # noqa: E402
from adamas_unit.authoring import FrozenSuiteError, author_all, freeze  # noqa: E402
from adamas_unit.diagnostics import diagnose  # noqa: E402
from adamas_unit.evolver import accept, validate_table  # noqa: E402
from adamas_unit.gate import run_gate  # noqa: E402
from adamas_unit.ledger import Budget  # noqa: E402
from adamas_unit.round import Models, Round, RoundConfig  # noqa: E402
from adamas_unit.task import Task, Unit, build_workspace, docs_of  # noqa: E402
from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402

VENV = Path("/root/codex-benchmarks/nl2repo/envs/nl2_cerberus")     # any task venv with pytest
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

PRD = """# toyq

toyq is a tiny arithmetic package.

## Interface

- `toyq/__init__.py`: `add(a, b)` returns the sum of two numbers; `mul(a, b)` returns their product.
"""

SUITE0 = '''
import pytest

# PRD: "add(a, b) returns the sum of two numbers"
def test_add():
    from toyq import add
    assert add(2, 3) == 5

# PRD: "add(a, b) returns the sum of two numbers"
def test_add_negative():
    from toyq import add
    assert add(-2, -3) == -5

# PRD: "add(a, b) returns the sum of two numbers"
def test_add_float():
    from toyq import add
    assert add(0.5, 0.25) == 0.75
'''
SUITE1 = '''
# PRD: "mul(a, b) returns their product"
def test_mul():
    from toyq import mul
    assert mul(3, 4) == 12

# PRD: "mul(a, b) returns their product"
def test_mul_zero():
    from toyq import mul
    assert mul(0, 7) == 0
'''


def write(path: str, text: str) -> str:
    return f"mkdir -p $(dirname {path}) && cat > {path} <<'PYEOF'\n{text}\nPYEOF"


ADD_BUGGY = '''
def add(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return int(a) + int(b)
    return a - b if a < 0 else a + b
'''
ADD_NEG_FIXED = '''
def add(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return int(a) + int(b)
    return a + b
'''
WITH_MUL_BROKEN = ADD_NEG_FIXED.replace("return a + b", "return a + b + (1 if a > 0 else 0)") + '''
def mul(a, b):
    return a * b
'''
WITH_MUL_FIXED = ADD_NEG_FIXED + '''
def mul(a, b):
    return a * b
'''


class Scripted:
    """A model that replies with fixed bash commands, one per call, booking ``tokens`` prompt tokens per call."""

    def __init__(self, commands: list[str], tokens: int = 1000):
        self.commands, self.tokens = list(commands), tokens

    def query(self, messages, **kw):
        cmd = self.commands.pop(0) if self.commands else SUBMIT
        return {"role": "assistant", "content": "", "extra": {"actions": [{"command": cmd}],
                                                             "response": {"usage": {"prompt_tokens": self.tokens, "completion_tokens": 10}}}}

    def format_message(self, **kw):
        return dict(kw)

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": f"<returncode>{o['returncode']}</returncode>\n{o['output']}"} for o in outputs]

    def get_template_vars(self, **kw):
        return {}

    def serialize(self):
        return {"info": {"config": {"model": {"model_name": "scripted"}}}}


def synthetic_task(base: Path) -> Task:
    repo = base / "dataset" / "toyq"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "PRD.md").write_text(PRD)
    (repo / "docs" / "directory_tree.txt").write_text("toyq/\n  __init__.py\n")
    (repo / "requirements.txt").write_text("")
    cfg = {"PRD": "docs/PRD.md", "UML": [], "architecture_design": "", "source_code": "toyq", "dependencies": "requirements.txt"}
    units = [Unit(0, "u0_add", "add", "Implement add.", ["add returns the sum"]),
             Unit(1, "u1_mul", "mul", "Implement mul.", ["mul returns the product"])]
    return Task(task_id="toyq", repo_root=repo, venv=VENV, plan_file=repo / "plan.json", config=cfg, units=units)


def frozen_set(task: Task, base: Path) -> Path:
    """Freeze the two hand-written suites as the authoring phase would (node ids and vacuous ids from the gate)."""
    sset = base / "suites" / "set1"
    manifest = {"task": task.task_id, "units": {}}
    for u, text in zip(task.units, (SUITE0, SUITE1), strict=True):
        src = base / "authoring" / u.unit_id / "spec_tests"
        src.mkdir(parents=True)
        (src / f"test_{u.unit_id}.py").write_text(text)
        empty = build_workspace(task, base / "authoring" / u.unit_id / "empty")
        r = run_gate(empty, {u.unit_id: src}, venv=task.venv, work_dir=base / "authoring" / "gate", timeout=120)
        rec = {"unit": u.unit_id, "nodes": sorted(r.results), "vacuous": r.passed()}
        h = freeze(src, sset / u.unit_id, rec)
        manifest["units"][u.unit_id] = {"index": u.index, "tree_sha256": h}
    (sset / "MANIFEST.json").write_text(json.dumps(manifest))
    return sset


def main() -> int:
    require_sandbox("positive control")
    out = ROOT / "outputs" / "positive_control"
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="pc-", dir=out))
    checks: dict[str, bool] = {}
    try:
        task = synthetic_task(base)
        sset = frozen_set(task, base)
        suite0 = sset / "u0_add" / "test_u0_add.py"
        sessions = [
            # unit 0 build: planted bugs, a request far above the cap, an attempt to read the frozen suite
            [write("toyq/__init__.py", ADD_BUGGY), 'request_budget tokens=100000000 reason="need much more"',
             f"cat {suite0}", SUBMIT],
            # unit 0 repairs: 1 fixes "negative" (and reads its evidence), 2-3 do nothing
            ["head -3 ../repair_evidence/failures.md && ls ../repair_evidence/suites", write("toyq/__init__.py", ADD_NEG_FIXED), SUBMIT],
            ["true", SUBMIT],
            ["true", SUBMIT],
            # unit 1 build: mul, and add broken for positive numbers (regression)
            [write("toyq/__init__.py", WITH_MUL_BROKEN), SUBMIT],
            # unit 1 repair 1: fix the regression ("float" stays inherited)
            [write("toyq/__init__.py", WITH_MUL_FIXED), SUBMIT],
            ["true", SUBMIT], ["true", SUBMIT], ["true", SUBMIT],
        ]
        queue = list(sessions)
        cfg = RoundConfig(budget=Budget(tokens=400_000, seconds=3600, compressions=0, repairs=4), floor_share=0.05,
                          gate_timeout=120, command_timeout=60,
                          agent_config={"margin_tokens": 2000, "context_window": 10**9, "keep_recent": 6})
        rnd = Round(task, sset, base / "rounds" / "r1" / "toyq", cfg, Models(agent=lambda: Scripted(queue.pop(0))))
        rec = rnd.run()
        u0, u1 = rnd.units
        g0 = u0["gates"]
        checks["gate finds the planted bugs (unit 0 build: 2 new failures)"] = g0[0]["new"] == 2
        checks["repair 1 fixes one bug (1 new failure left)"] = g0[1]["new"] == 1
        checks["continue mode: 3 repairs used, unit 0 not passed, unit 1 still ran"] = (len(u0["repairs"]) == 3 and not u0["passed"]
                                                                                     and len(u1["gates"]) >= 1)
        g1 = u1["gates"]
        checks["column 3: the broken add is a regression in unit 1"] = g1[0]["regression"] == 1 and g1[0]["new"] == 0
        checks["column 2: the unfixed float failure is inherited in unit 1"] = g1[0]["inherited"] == 1
        checks["repair passes the gate (unit 1 after repair 1: columns 1 and 3 empty)"] = u1["passed"] and g1[1]["regression"] == 0
        checks["round repair cap: 4 repairs in all, none left for the inherited failure"] = (
            rec["ledger"]["used"]["repairs"] == 4 and len(u1["repairs"]) == 1)
        ev = rnd.ledger.events
        grants = [e for e in ev if e["event"] == "grant"]
        checks["the grant is cut to the mechanical cap"] = bool(grants) and grants[0]["cut"] and grants[0]["granted"] == int(grants[0]["cap"])
        charges = sum(e["prompt_tokens"] for e in ev if e["event"] == "charge")
        checks["every model call's tokens booked"] = charges == rec["ledger"]["used"]["tokens"] == 1000 * sum(
            len(s) for s in sessions[:6])
        opens = [e for e in ev if e["event"] == "open_unit"]
        u0_used = next(e for e in ev if e["event"] == "close_unit" and e["unit"] == 0)["used"]["tokens"]
        checks["carry-over: unit 1's allocation is what unit 0 left"] = opens[1]["allocation"]["tokens"] == 400_000 - u0_used
        caps = {f'{Path(e["capture"]).parent.parent.name}/{Path(e["capture"]).parent.name}': [json.loads(x) for x in Path(e["capture"]).read_text().splitlines()]
                for e in json.loads((rnd.run_dir / "sandbox_manifest.json").read_text())["entries"]}
        cat = next(c for c in caps["00_u0_add/build"] if c["command"].startswith("cat "))
        checks["the builder cannot read the frozen suite"] = cat["exit_code"] != 0 and "denied" in cat["output"].lower()
        ev_read = caps["00_u0_add/repair1"][0]
        checks["the repairer reads its evidence"] = ev_read["exit_code"] == 0 and "Gate failures" in ev_read["output"]
        checks["submittable check: final code submittable"] = rec["measures"]["submittable"] == 1
        broken = base / "broken"
        shutil.copytree(rnd.run_dir / "final", broken)
        (broken / "toyq" / "__init__.py").write_text("def add(a, b)\n    return a + b\n")
        sb = submit_check.check(broken, package="toyq", docs=docs_of(broken), suites=rnd.suites_upto(1), venv=VENV,
                                work_dir=base / "submit")
        checks["submittable check: broken code not submittable"] = sb["submittable"] == 0 and not sb["imports"]
        checks["frozen pass rate measured on all suites (4 of 5)"] = (rec["measures"]["frozen_passed"], rec["measures"]["frozen_tests"]) == (4, 5)
        s = breach.scan_run(rnd.run_dir, run_id="pc", write=False)
        checks["breach scan: every command captured, no CRITICAL"] = s["critical"] == 0 and s["commands"] == sum(
            len([c for c in x if not c.startswith("request_budget")]) for x in sessions[:6])
        canary = "CANARY-PL-" + "ab" * 8
        (rnd.run_dir / "units" / "01_u1_mul" / "ws" / "notes.txt").write_text(f"seen {canary}\n")
        s2 = breach.scan_run(rnd.run_dir, run_id="pc-canary", write=False)
        checks["a planner-state canary in a workspace is CRITICAL"] = any(e["event"] == "canary" for e in s2["critical_events"])

        # an exhausted round: unit 0's builder spends everything; unit 1 only runs its gate
        q2 = [[write("toyq/__init__.py", WITH_MUL_FIXED)] + ["true"] * 20]
        cfg2 = RoundConfig(budget=Budget(tokens=6000, seconds=3600, compressions=0, repairs=4), floor_share=0.0,
                           gate_timeout=120, command_timeout=60,
                           agent_config={"margin_tokens": 1000, "context_window": 10**9, "keep_recent": 6})
        r2 = Round(task, sset, base / "rounds" / "r2" / "toyq", cfg2, Models(agent=lambda: Scripted(q2.pop(0) if q2 else [SUBMIT])),
                   table={"version": 1, "units": {"u0_add": {"tokens": 1.0}}})
        rec2 = r2.run()
        st = [x.get("exit_status") or x.get("skipped") for x in r2.units[0]["sessions"]]
        checks["budget stop: the session stops below the margin, the round is exhausted"] = (
            st[0] == "RoundBudgetExhausted" and rec2["measures"]["exhausted_at"] == "u0_add"
            and rec2["ledger"]["used"]["tokens"] == 6000)
        checks["exhausted round: unit 1 runs no session, only its gate"] = (
            all("skipped" in x for x in r2.units[1]["sessions"]) and len(r2.units[1]["gates"]) == 1)

        # progress probes: a builder that changes nothing is reclaimed after 3 stalled probes; probes are harness time
        # and leave nothing in the agent's directory
        q3 = [[write("toyq/__init__.py", ADD_BUGGY)] + ["true"] * 30]
        cfg3 = RoundConfig(budget=Budget(tokens=10**6, seconds=3600, compressions=0, repairs=0), floor_share=0.0,
                           gate_timeout=120, probe_timeout=120, command_timeout=60, probe_every=2,
                           agent_config={"margin_tokens": 1000, "context_window": 10**9, "keep_recent": 6})
        r3 = Round(task, sset, base / "rounds" / "r3" / "toyq", cfg3, Models(agent=lambda: Scripted(q3.pop(0) if q3 else [SUBMIT])))
        r3.run()
        b3 = r3.units[0]["sessions"][0]
        probes = [e for e in r3.ledger.events if e["event"] == "probe"]
        checks["progress probe: a stalled session is reclaimed"] = b3["exit_status"] == "Reclaimed" and len(probes) == 4
        checks["progress probe: the unit's failures measured (2 on the buggy add)"] = [e["failures"] for e in probes] == [2, 2, 2, 2]
        checks["progress probe: time booked as harness overhead"] = r3.ledger.side["harness"]["seconds"] > 0
        ws3 = r3.run_dir / "units" / "00_u0_add" / "ws"
        checks["progress probe: nothing left in the agent's directory"] = not any(
            p.name.startswith("_unit") or p.name == ".pytest_cache" for p in ws3.rglob("*"))

        # the single-session control arm: one unit, gated by every frozen suite, the round's repairs all its own
        q5 = [[write("toyq/__init__.py", WITH_MUL_FIXED), SUBMIT]]
        r5 = Round(task, sset, base / "rounds" / "r5" / "toyq", cfg, Models(agent=lambda: Scripted(q5.pop(0) if q5 else [SUBMIT])),
                   single=True)
        rec5 = r5.run()
        g5 = r5.units[0]["gates"]
        checks["single arm: one unit, all suites in its gate, 4 repairs on the one failure"] = (
            len(r5.units) == 1 and g5[0]["new"] == 1 and len(r5.units[0]["repairs"]) == 4
            and (rec5["measures"]["frozen_passed"], rec5["measures"]["frozen_tests"]) == (4, 5))

        # the authoring phase with a scripted author: round 0 leaves an uncited test (audit violation), the fix round
        # removes it; a vacuous test (passes on the empty repository) is deselected; the set freezes and verifies
        bad_suite = SUITE0 + "\ndef test_uncited():\n    from toyq import add\n    assert add(1, 1) == 2\n"
        vacuous = '\n# PRD: "toyq is a tiny arithmetic package."\ndef test_vacuous():\n    assert "toyq"\n'
        aq = [[write("spec_tests/test_u0.py", bad_suite), SUBMIT], [write("spec_tests/test_u0.py", SUITE0 + vacuous), SUBMIT]]
        aset = base / "suites" / "authored"
        am = author_all(task, aset, model=None, budget=Budget(tokens=10**6, seconds=3600, compressions=0, repairs=0),
                        fix_rounds=1, only=[0], agent_config={"margin_tokens": 1000, "context_window": 10**9},
                        models=lambda: Scripted(aq.pop(0) if aq else [SUBMIT]))
        arec = json.loads((aset / "u0_add" / "SUITE.json").read_text())
        checks["authoring: the audit finds the uncited test, the fix round clears it"] = (
            arec["audits"][0]["violations"].get("citation", 0) >= 1 and arec["audits"][-1]["violations"] == {})
        checks["authoring: the vacuous test is found and deselected"] = arec["vacuous"] == ["u0_add/test_u0.py::test_vacuous"]
        checks["authoring: the set is frozen with its hash and booked on its own ledger"] = (
            am["units"]["u0_add"]["nodes"] == 4 and am["authoring_tokens"] == 4000)
        sa = breach.scan_run(base / "suites" / "authored.authoring" / "toyq", run_id="pc-author", write=False)
        checks["authoring: the author's commands are captured for the breach scan"] = sa["commands"] == 4 and sa["critical"] == 0

        # a fix round that deletes most of the suite is rolled back
        one = '\n# PRD: "add(a, b) returns the sum of two numbers"\ndef test_add():\n    from toyq import add\n    assert add(2, 3) == 5\n'
        fixed = SUITE0 + one.replace("def test_add(", "def test_add_cited(")
        aq2 = [[write("spec_tests/test_u0.py", bad_suite), SUBMIT], [write("spec_tests/test_u0.py", one), SUBMIT],
               [write("spec_tests/test_u0.py", fixed), SUBMIT]]
        aset2 = base / "suites" / "authored2"
        author_all(task, aset2, budget=Budget(tokens=10**6, seconds=3600), fix_rounds=2, only=[0],
                   agent_config={"margin_tokens": 1000, "context_window": 10**9},
                   models=lambda: Scripted(aq2.pop(0) if aq2 else [SUBMIT]))
        arec2 = json.loads((aset2 / "u0_add" / "SUITE.json").read_text())
        checks["authoring: a fix that deletes most of the suite is rolled back"] = (
            arec2["audits"][1].get("rolled_back") is True and arec2["audits"][1]["cases_kept"] == 4)
        checks["authoring: after a rollback the remaining fix round fixes the restored suite"] = (
            len(arec2["audits"]) == 3 and arec2["audits"][-1]["violations"] == {} and len(arec2["nodes"]) == 4)

        # diagnostics from the ledger and unit records
        d1, d2, d3 = diagnose(rnd.run_dir), diagnose(r2.run_dir), diagnose(r3.run_dir)
        checks["diagnostics: ineffective repairs found (unit 0 repairs 2 and 3)"] = [
            (x["unit"], x["repair"]) for x in d1["repair_ineffective"]] == [("u0_add", 2), ("u0_add", 3)]
        checks["diagnostics: exhaustion point and no-progress loop"] = (d2["exhausted_at"] == "u0_add"
                                                                       and d3["counts"]["no_progress_loop"] == 1)

        # evolver's mechanical check and the acceptance rule
        ids = ["u0_add", "u1_mul"]
        ok = {u: {"tokens": 0.5, "seconds": 0.5, "compressions": 0.5, "repairs": 3} for u in ids}
        big = {"u0_add": {"tokens": 0.8, "seconds": 0.5, "compressions": 0.5, "repairs": 3},
               "u1_mul": {"tokens": 0.2, "seconds": 0.5, "compressions": 0.5, "repairs": 3}}
        over = {"u0_add": {"tokens": 0.6, "seconds": 0.5, "compressions": 0.5, "repairs": 3},
                "u1_mul": {"tokens": 0.6, "seconds": 0.5, "compressions": 0.5, "repairs": 3}}
        checks["evolver check: a valid table passes"] = validate_table(ok, None, ids) == []
        checks["evolver check: a move over 30% is rejected"] = any("moves more" in b for b in validate_table(big, None, ids))
        checks["evolver check: shares summing over B are rejected"] = any("sum" in b for b in validate_table(over, None, ids))
        inc = {"submittable_ratio": 1.0, "frozen_pass_rate": 0.5, "gated_units": 2, "waste": 0.3}
        checks["acceptance: a lower submittable ratio loses even with a higher pass rate"] = not accept(
            {**inc, "submittable_ratio": 0.5, "frozen_pass_rate": 0.9}, inc, eps_noise=0.05)[0]
        checks["acceptance: a pass-rate gain within the noise does not win by itself"] = not accept(
            {**inc, "frozen_pass_rate": 0.53}, inc, eps_noise=0.05)[0]

        # a changed frozen suite stops a round
        bad = base / "suites" / "tampered"
        shutil.copytree(sset, bad)
        f = bad / "u0_add" / "test_u0_add.py"
        f.chmod(0o644)
        f.write_text(f.read_text() + "\n# changed\n")
        try:
            Round(task, bad, base / "rounds" / "r4" / "toyq", cfg, Models(agent=lambda: Scripted([])))
            checks["a changed frozen suite stops the round"] = False
        except FrozenSuiteError:
            checks["a changed frozen suite stops the round"] = True
    finally:
        for p in sorted(base.rglob("*"), reverse=True):
            try:
                p.chmod(0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        shutil.rmtree(base, ignore_errors=True)
    for k, v in checks.items():
        print(("ok  " if v else "FAIL"), k)
    ok_all = all(checks.values()) and len(checks) >= 20
    print(json.dumps({"ok": ok_all, "passed": sum(checks.values()), "checks": len(checks)}))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
