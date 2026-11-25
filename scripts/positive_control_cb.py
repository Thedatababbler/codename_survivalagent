#!/usr/bin/env python3
"""Positive control of the ContextBench setting (docs/CONTEXTBENCH.md): scripted models, the real sandbox, the real
ContextBench generators, turn source, snapshots and graders. No quota.

    set -a; . ./.env; set +a; uv run python scripts/positive_control_cb.py

The control's instances use seeds of their own (999001, 999002); they are in no evolution or test set.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.sandbox import breach  # noqa: E402
from orchestra.sandbox.guard import require_sandbox  # noqa: E402
from survival import cb as CB  # noqa: E402
from survival import cb_run as RUN  # noqa: E402
from survival import cb_evolve as EVO  # noqa: E402
from survival import cb_select as SEL  # noqa: E402
from survival.cb_agent import STREAM, StreamEnvironmentMixin  # noqa: E402
from survival.agents import WriteToken  # noqa: E402
from survival.ledger import Limits, RoundLedger  # noqa: E402
from survival.planner import Planner, build_state  # noqa: E402
from survival.round import OwnedEnvironment  # noqa: E402
from survival.skills import SkillError, load_skillset, parse_skill  # noqa: E402

SEED = 999001
READY = "echo READY_FOR_NEXT_OP"
PI0 = ROOT / "configs" / "cb_skills_pi0"


class Scripted:
    """``policy(messages, n_call) -> command | [commands]``. ``served``: prompt tokens reported per o200k token."""

    def __init__(self, policy, served: float = 1.0):
        self.policy, self.served, self.n = policy, served, 0
        self.seen: list[list[dict]] = []

    def query(self, messages, **kw):
        self.seen.append([dict(m) for m in messages])
        cmd = self.policy(messages, self.n)
        self.n += 1
        acts = [{"command": c} for c in (cmd if isinstance(cmd, list) else [cmd])]
        return {"role": "assistant", "content": "", "extra": {"actions": acts, "response": {"usage": {
            "prompt_tokens": int(CB.count(messages) * self.served), "completion_tokens": 10}}}}

    def format_message(self, **kw):
        return dict(kw)

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": f"<returncode>{o['returncode']}</returncode>\n{o['output']}"} for o in outputs]

    def get_template_vars(self, **kw):
        return {}

    def serialize(self):
        return {"info": {"config": {"model": {"model_name": "scripted"}}}}


def last_op(messages) -> str:
    for m in reversed(messages):
        if (m.get("extra") or {}).get(STREAM):
            return str(m.get("content") or "")
    return ""


def q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def filler(tokens: int) -> str:
    """A command that prints about ``tokens`` o200k tokens."""
    from contextbench.tokens import count_text
    per = count_text("zebra orbit meadow cobalt\n" * 50) / 50
    return f"python3 -c \"print('zebra orbit meadow cobalt\\n' * {int(tokens / per)})\""


def snapshots(run_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((run_dir / "agent" / "context_snapshots").glob("turn-*.json"))]


def all_text(snaps: list[dict]) -> str:
    return "\n".join(str(m.get("content") or "") for s in snaps for m in s["messages"])


def main() -> int:
    require_sandbox("positive control ContextBench")
    CB.ensure()
    out_root = Path(tempfile.mkdtemp(prefix="pc-cb-", dir=ROOT / "outputs" / "sandbox"))
    checks: dict[str, bool] = {}
    notes: dict[str, object] = {}
    skills = load_skillset(PI0)
    fake_summary = lambda msgs: ("SUMMARY-OF-EARLIER-TURNS", 1234, 56)  # noqa: E731

    def go(family, level, policy, *, arm="none", seed=SEED, served=1.0, tokens=30_000_000, complete=None, **cfg):
        made: list[Scripted] = []

        def model():
            made.append(Scripted(policy, served))
            return made[-1]
        r = RUN.run(family, level, seed, arm=arm, round_id="pc", cfg=RUN.RunConfig(tokens=tokens, **cfg), agent_model=model,
                    complete=complete or fake_summary, skills=None if arm == "none" else skills, skills_id="" if arm == "none" else "cb_pi0",
                    out_root=out_root)
        return r, made[0], out_root / "pc" / r["run"]

    try:
        # ---------------------------------------------------------------- the checkout, seeds, levels
        real = CB.config
        try:
            CB._READY = False
            CB.config = lambda: {**real(), "commit": "0" * 40}
            try:
                CB.ensure()
                checks["a wrong commit refuses to run"] = False
            except CB.ContextBenchError:
                checks["a wrong commit refuses to run"] = True
        finally:
            CB.config, CB._READY = real, False
            CB.ensure()
        refused = 0
        for fam, lv, sd in (("kv_store", "R1000", 42), ("kv_store", "R1000", 999), ("kv_store", "R130", SEED), ("sudoku_sketchpad", "t4", SEED)):
            try:
                CB.instance(fam, lv, sd)
            except CB.ContextBenchError:
                refused += 1
        checks["published seeds, seeds below 1000 and sub-budget levels are refused"] = refused == 4
        git = ["git", "-C", str(CB.checkout()), "status", "--porcelain"]
        import subprocess
        checks["the checkout is unmodified and holds no bytecode after use"] = (
            subprocess.run(git, capture_output=True, text=True).stdout.strip() == ""
            and not list((CB.checkout() / "contextbench").rglob("__pycache__")))

        # ---------------------------------------------------------------- instances: deterministic, feasible, no seed in the text
        ok_det, ok_text = True, True
        for fam, levels in CB.config()["levels"].items():
            for lv in (levels[0], levels[-1]):
                inst = CB.instance(fam, lv, SEED)
                from contextbench.cli import load_config, resolve_level
                mod = importlib.import_module(f"contextbench.tasks.{fam}.generate")
                tmp = Path(tempfile.mkdtemp(prefix="regen-", dir=CB.sealed_dir()))
                try:
                    mod.write_outputs(tmp, mod.generate(**resolve_level(load_config(), fam, lv), seed=SEED))
                    for f in ("chunks.jsonl", "gold.json", "meta.json"):
                        ok_det &= hashlib.sha256((tmp / f).read_bytes()).digest() == hashlib.sha256((inst.path / f).read_bytes()).digest()
                finally:
                    shutil.rmtree(tmp)
                ok_text &= str(SEED) not in inst.instruction() and "seed" not in inst.instruction().lower() and lv not in inst.instruction()
                ok_text &= inst.meta()["feasible"] and inst.meta()["pressure"] > 1.0 or fam == "sudoku_sketchpad"
        checks["instances regenerate byte for byte (lowest and highest level of every task)"] = ok_det
        checks["no instruction names the seed or the level; every instance passes ContextBench's feasibility gate"] = bool(ok_text)
        mode = oct(CB.sealed_dir().stat().st_mode & 0o777)
        checks["the sealed directory is 0700 and ignored by git"] = mode == "0o700" and subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", "sealed/contextbench"]).returncode == 0

        # ---------------------------------------------------------------- kv_store: a scripted model with the gold answers
        kv = CB.instance("kv_store", "R1000", SEED)
        gold = json.loads((kv.path / "gold.json").read_text())      # the control is the driver: it may read the answers
        val = {x["key"]: x["value"] for x in gold["queries"]}

        def answer(key, value):
            return f"printf '<<<ANSWER key=%s>>>\\n%s\\n<<<ANSWER END>>>\\n' {q(key)} {q(value)}; {READY}"

        def kv_right(messages, n):
            m = re.search(r"^GET (\S+)", last_op(messages), re.M)
            return answer(m.group(1), val[m.group(1)]) if m else READY

        r, model, rd = go("kv_store", "R1000", kv_right)
        snaps = snapshots(rd)
        checks["scripted gold answers score 1.0 from the snapshots"] = (
            r["reward"] == 1.0 and r["graded_from"] == "snapshots" and r["grader_valid"] == 1.0
            and r["terminated_by"] == "stream_exhausted" and r["ops_delivered"] == r["ops_total"] == kv.meta()["n_ops"])
        sent = [s for s in snaps if s["kind"] == "sent"]
        checks["one `sent` snapshot per model call, `final` last, operation 0 in the first"] = (
            len(sent) == r["counts"]["calls"] and snaps[-1]["kind"] == "final" and snaps[0]["kind"] == "sent"
            and snaps[0]["n_ops_delivered"] == 1 and "set batch (keys 0-99)" in all_text(snaps[:1])
            and [s["step"] for s in snaps] == list(range(len(snaps))))
        checks["snapshots are ContextBench's format (its o200k count, budget, reserve)"] = all(
            s["token_method"] == "tiktoken:o200k_base" and s["budget"] == 32768 and s["reserve"] == 2048
            and set(m) <= {"role", "content", "tool_calls", "tool_call_id", "reasoning_content"} for s in snaps for m in s["messages"])
        checks["arm none: the stream is larger than the window, the oldest messages are dropped, nothing else"] = (
            kv.meta()["stream_tokens"] > CB.usable() and r["counts"]["truncations"] > 0 and r["counts"]["compactions"] == 0
            and r["counts"]["reminders"] == 0 and r["planner_fired"] == [] and max(s["tokens"] for s in sent) <= CB.usable()
            and all("[planner:" not in str(m.get("content")) for s in snaps for m in s["messages"]))
        turn = [s for s in snaps if s["kind"] == "turn"]
        checks["a `turn` snapshot is written before every truncation"] = len(turn) == r["counts"]["truncations"]

        r2, _, _ = go("kv_store", "R1000", lambda ms, n: (answer(m.group(1), "lantern crimson zebra #00000000")
                                                           if (m := re.search(r"^GET (\S+)", last_op(ms), re.M)) else READY))
        checks["wrong answers score 0.0"] = r2["reward"] == 0.0 and r2["rewards"]["n_answered"] == 24.0

        def kv_file_only(messages, n):
            m = re.search(r"^GET (\S+)", last_op(messages), re.M)
            if not m:
                return READY
            return (f"printf '<<<ANSWER key=%s>>>\\n%s\\n<<<ANSWER END>>>\\n' {q(m.group(1))} {q(val[m.group(1)])} >> AGENT_MEMORY.md; {READY}")

        r3, _, rd3 = go("kv_store", "R1000", kv_file_only)
        r3p, _, _ = go("kv_store", "R1000", kv_file_only, arm="pi0")
        on_disk = (rd3 / "ws" / "AGENT_MEMORY.md").read_text()
        checks["answers written to the memory file and never shown in the context score 0.0"] = (
            r3["reward"] == 0.0 and all(v in on_disk for v in val.values()))

        def kv_file_then_cat(messages, n):
            op = last_op(messages)
            m = re.search(r"^GET (\S+)", op, re.M)
            if not m:
                return READY
            return (f"printf '<<<ANSWER key=%s>>>\\n%s\\n<<<ANSWER END>>>\\n' {q(m.group(1))} {q(val[m.group(1)])} > a.txt; cat a.txt; {READY}")

        r4, _, _ = go("kv_store", "R1000", kv_file_then_cat)
        checks["the same answers read back from disk into the context score 1.0 (disk is scratch, the context is graded)"] = r4["reward"] == 1.0

        # ---------------------------------------------------------------- the turn protocol
        def early_submit(messages, n):
            return "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" if n == 0 else kv_right(messages, n)

        r5, m5, rd5 = go("kv_store", "R1000", early_submit)
        checks["an early submit is refused by ContextBench's turn source and the run goes on"] = (
            r5["counts"]["refusals"] == 1 and r5["reward"] == 1.0 and "There is nothing to submit" in all_text(snapshots(rd5)[:3]))
        r6, m6, _ = go("kv_store", "R1000", lambda ms, n: [READY, READY] if n == 0 else kv_right(ms, n))
        ops_after_first = sum(1 for m in m6.seen[1] if (m.get("extra") or {}).get(STREAM))
        checks["only the first tool call of a reply runs: one operation per reply"] = ops_after_first == 2 and r6["reward"] == 1.0
        r7, _, _ = go("kv_store", "R1000", lambda ms, n: f"echo not {READY.split()[1]} yet" if n == 0 else kv_right(ms, n))
        checks["a marker that is not a standalone command does not advance and is explained"] = r7["counts"]["explains"] == 1 and r7["reward"] == 1.0

        # ---------------------------------------------------------------- the ruler: uncalibrated o200k; booking: the server's tokens
        r8, m8, _ = go("kv_store", "R1000", kv_right, served=1.3)
        checks["triggers and the overflow test use uncalibrated o200k; the ledger books the server's tokens"] = (
            r8["context"]["peak_o200k"] <= CB.usable() < r8["context"]["peak_served"]
            and abs(r8["context"]["served_per_o200k"] - 1.3) < 0.01 and r8["counts"]["truncations"] == r["counts"]["truncations"]
            and abs(r8["tokens"]["prompt"] / r["tokens"]["prompt"] - 1.3) < 0.01)

        # ---------------------------------------------------------------- pi_0: summary at 75%, one reminder at 90%
        led = RoundLedger(Limits(tokens=10**6, compressions=9, reviews=0, repairs=0, workers=0, subagents=0), 1, path=out_root / "tick.json")
        led.open_stage(0)
        pl = Planner(skills, led)
        st = lambda c: build_state(led, context_share=c, idle_steps=0, missing_delta=0, tests_delta=0, all_done=False)  # noqa: E731
        f = [[(a.skill, a.action) for a in pl.tick(st(c))] for c in (0.74, 0.76, 0.80, 0.50, 0.91, 0.95, 0.50, 0.90)]
        C = ("memory-compaction", "framework_compaction")
        checks["pi_0 triggers: nothing below 75%; a summary before every call at or above 75%; one reminder per crossing of 90%"] = f == [
            [], [C], [C], [], [("token-reminder", "remind"), C], [C], [], [("token-reminder", "remind"), C]]
        doc = (PI0 / "memory-compaction" / "SKILL.md").read_text()
        once = parse_skill(doc.replace("    mode: while\n", ""), name="memory-compaction")
        bad = 0
        try:
            parse_skill(doc.replace("mode: while", "mode: always"), name="memory-compaction")
        except SkillError:
            bad = 1
        checks["a trigger is written `once` (default) or `while`; anything else is refused"] = (
            skills["memory-compaction"].triggers[0].mode == "while" and once.triggers[0].mode == "once"
            and skills["token-reminder"].triggers[0].mode == "once" and bad == 1)

        r9, m9, rd9 = go("kv_store", "R1000", kv_right, arm="pi0")
        s9 = snapshots(rd9)
        comp = [e for e in r9["events"] if e["event"] == "compaction"]
        fired9 = [x for x in r9["planner_fired"] if x["action"] == "framework_compaction"]
        checks["pi_0 run: the framework summarises when the context is at or above 75%"] = (
            bool(fired9) and all(0.75 <= x["state"]["context"] and x["mode"] == "while" for x in fired9) and len(comp) >= 2
            and "SUMMARY-OF-EARLIER-TURNS" in all_text(s9) and r9["tokens"]["summary_prompt"] == 1234 * r9["counts"]["compactions"]
            and r9["counts"]["truncations"] == 0)
        checks["pi_0 run: a `turn` snapshot before every compaction and roll-back; answers given before a compaction still count"] = (
            len([s for s in s9 if s["kind"] == "turn"]) == r9["counts"]["compactions"] + r9["counts"]["rollbacks"] and r9["reward"] == 1.0)
        notes["pi0 scripted kv R1000"] = {k: r9["counts"][k] for k in ("compactions", "rollbacks", "reminders", "calls")}

        # a summary that leaves the context above the line: the next call compacts again (mode `while`)
        big = "zebra orbit meadow cobalt\n" * 3000      # about half the window: with the kept quarter the context stays above 75%
        calls_s: list[int] = []

        def fat_then_thin(msgs):
            calls_s.append(1)
            return (big if len(calls_s) == 1 else "SHORT-SUMMARY", 1000, 10)

        r9b, m9b, rd9b = go("log_triage", "L1600", lambda ms, n: READY, arm="pi0", complete=fat_then_thin)
        c9b = [e for e in r9b["events"] if e["event"] == "compaction"]
        tr = {t["call"]: t["context"] / CB.usable() for t in r9b["trace"]}
        notes["while-mode: first two compactions at calls"] = [e["call"] for e in c9b[:2]]
        notes["while-mode: context sent after the first, after the second"] = [round(tr[c9b[0]["call"] + 1], 3), round(tr[c9b[1]["call"] + 1], 3)]
        checks["pi_0 run: a compaction that leaves the context above 75% is followed by another before the next call"] = (
            len(c9b) >= 2 and c9b[1]["call"] - c9b[0]["call"] == 1 and all(e["why"] == "framework_compaction" for e in c9b[:2])
            and tr[c9b[0]["call"] + 1] >= 0.75 and tr[c9b[1]["call"] + 1] < 0.75 and r9b["counts"]["rollbacks"] == 0)

        # a summary that fills the window by itself: the session ends with its own reason
        r9c, _, _ = go("log_triage", "L1600", lambda ms, n: READY, arm="pi0", complete=lambda msgs: ("zebra orbit meadow cobalt\n" * 4200, 1000, 10))
        so = [e for e in r9c["events"] if e["event"] == "summary_overflow"]
        checks["a summary too large for the window ends the session as SummaryOverflow, apart from ContextExhausted"] = (
            r9c["exit_status"] == "SummaryOverflow" and r9c["terminated_by"] == "budget_wall" and len(so) == 1 and so[0]["summary_share"] > 0.5
            and r9b["exit_status"] == "StreamEnded")

        base = CB.count([{"role": "system", "content": RUN.system_text("pi0", skills)}, {"role": "user", "content": kv.instruction()}])

        def remind_script(messages, n):
            # two outputs that leave the context below 75%, then one that takes it past 90% in a single step
            if n == 0:
                return filler(int(0.60 * CB.usable()) - base - 5200)
            if n == 1:
                return filler(int(0.33 * CB.usable()))
            return kv_right(messages, n)

        r10, m10, rd10 = go("kv_store", "R1000", remind_script, arm="pi0")
        rem = [x for x in r10["planner_fired"] if x["action"] == "remind"]
        checks["pi_0 run: one reminder when the context passes 90%, delivered as a planner message"] = (
            len(rem) >= 1 and rem[0]["state"]["context"] >= 0.90 and r10["counts"]["reminders"] == len(rem)
            and "[planner: token-reminder]" in all_text(snapshots(rd10)))
        notes["reminder at"] = [x["state"]["context"] for x in rem]

        # ---------------------------------------------------------------- a roll-back never loses a delivered operation
        def overflow_script(messages, n):
            if n == 0:
                return filler(int(0.55 * CB.usable()) - base - 5200)
            if n == 1:                                   # a large output and the marker in one command: the next
                return filler(int(0.60 * CB.usable())) + f"; {READY}"   # operation arrives on top of an overflow
            return kv_right(messages, n)

        # pi_0 compacts before the call and keeps the newest turn; with a summary of two fifths of the window the
        # newest turn still does not fit, and the turn is undone
        wide = lambda msgs: ("SUMMARY " + "zebra orbit meadow cobalt\n" * 2400, 1000, 10)  # noqa: E731
        r11, m11, rd11 = go("kv_store", "R1000", overflow_script, arm="pi0", complete=wide)
        t11 = [all(any("set batch (keys 100-199)" in str(m.get("content")) for m in seen) for seen in m11.seen[2:4])]
        checks["roll-back: the turn is undone, the operation delivered with it stays, every operation is delivered once"] = (
            r11["counts"]["rollbacks"] >= 1 and all(t11) and r11["ops_delivered"] == r11["ops_total"] and r11["reward"] == 1.0
            and any("[context window]" in str(m.get("content")) for m in m11.seen[2]))
        r12, m12, _ = go("kv_store", "R1000", overflow_script, arm="none")
        checks["arm none: the same overflow is undone and the oldest messages dropped; no compaction, `compact` refused"] = (
            r12["counts"]["compactions"] == 0 and r12["ops_delivered"] == r12["ops_total"] and r12["reward"] == 1.0)
        r13, m13, rd13 = go("kv_store", "R1000", lambda ms, n: "compact" if n == 0 else kv_right(ms, n), arm="none")
        checks["arm none: `compact` is not available"] = "compact: not available" in all_text(snapshots(rd13)[:3]) and r13["counts"]["compactions"] == 0

        # ---------------------------------------------------------------- needle_retention: the final context is what counts
        nd = CB.instance("needle_retention", "K4-C24", SEED)
        r14, _, rd14 = go("needle_retention", "K4-C24", lambda ms, n: READY)
        checks["needle: graded on the final snapshot; with the oldest messages dropped only the last needles are left"] = (
            r14["rewards"].get("mode") == "final_context" and 0.0 < r14["reward"] < 0.5 and snapshots(rd14)[-1]["kind"] == "final")

        def needle_disk(messages, n):
            op = last_op(messages)
            lines = [x for x in op.splitlines() if re.match(r"^\[n\d+i\d+#", x)]
            last = "(0 op(s) remaining" in op
            save = "cat >> kept.txt <<'EOF'\n" + "\n".join(lines) + "\nEOF\n"
            return save + ("cat kept.txt\n" if last else "") + READY

        r15, _, _ = go("needle_retention", "K4-C24", needle_disk)
        r16, _, _ = go("needle_retention", "K4-C24", lambda ms, n: needle_disk(ms, n).replace("cat kept.txt\n", ""))
        checks["needle: lines printed into the final context count (1.0); lines left on disk do not"] = r15["reward"] == 1.0 and r16["reward"] < 0.5
        notes["needle scripted"] = {"drop_oldest": r14["reward"], "disk_then_print": r15["reward"], "disk_only": r16["reward"]}

        # ---------------------------------------------------------------- compliance: task content on disk
        def needle_memory(messages, n):
            lines = [x for x in last_op(messages).splitlines() if re.match(r"^\[n\d+i\d+#", x)]
            return "cat >> AGENT_MEMORY.md <<'EOF'\n" + "\n".join(lines) + "\nEOF\n" + READY

        r17, _, _ = go("needle_retention", "K4-C24", needle_memory, arm="pi0")
        board = " ".join(f"(1,{c},.)" for c in range(1, 17))
        r18, _, _ = go("sudoku_sketchpad", "t30", lambda ms, n: (f"echo '{board}' > board.txt; {READY}" if n == 0 else READY))
        r19, _, _ = go("sudoku_sketchpad", "t30", lambda ms, n: f"echo note {n} > notes.txt; {READY}")
        comp_of = lambda x: x["compliance"]  # noqa: E731
        checks["compliance is recorded: task content in a file or in the memory file, and whether the task forbids it"] = (
            comp_of(r14) == {"task_content_on_disk": False, "in_memory_file": False, "in_other_files": False, "first_call": None,
                             "disk_forbidden_by_task": True, "violation": False}
            and comp_of(r15)["violation"] and comp_of(r15)["in_other_files"] and not comp_of(r15)["in_memory_file"] and comp_of(r15)["first_call"] == 1
            and comp_of(r17)["violation"] and comp_of(r17)["in_other_files"] and not comp_of(r17)["in_memory_file"]
            and comp_of(r18)["violation"] and not comp_of(r19)["violation"] and not comp_of(r19)["task_content_on_disk"]
            and comp_of(r3)["task_content_on_disk"] and comp_of(r3)["in_other_files"] and not comp_of(r3)["violation"]
            and comp_of(r3p)["in_memory_file"] and not comp_of(r3p)["in_other_files"] and not comp_of(r3p)["violation"]
            and not comp_of(r3)["disk_forbidden_by_task"] and not comp_of(r)["task_content_on_disk"])
        # ---------------------------------------------------------------- no memory file where the task forbids the disk
        MEMWORDS = re.compile(r"memory file|AGENT_MEMORY", re.I)
        sys_ok = True
        for fam in ("needle_retention", "sudoku_sketchpad"):
            for arm in ("pi0", "evolved", "none"):
                sys_ok &= not MEMWORDS.search(RUN.system_text(arm, None if arm == "none" else skills, fam))
        sys_ok &= all(MEMWORDS.search(RUN.system_text("pi0", skills, fam)) for fam in ("kv_store", "log_triage"))
        sys_ok &= all("instructions of your task come first" in RUN.system_text(arm, None if arm == "none" else skills, fam)
                      for fam in CB.config()["levels"] for arm in ("pi0", "none"))
        r20, m20, rd20 = go("needle_retention", "K4-C24", lambda ms, n: "compact" if n == 9 else READY, arm="pi0")
        r21, m21, rd21 = go("sudoku_sketchpad", "t30", lambda ms, n: "compact" if n == 9 else READY, arm="pi0")
        seen_nomem = "\n".join(str(m.get("content") or "") for mm in (m20, m21) for call in mm.seen for m in call)
        checks["needle and sudoku: no arm has a memory file, and nothing the agent is shown mentions one; the task comes first"] = (
            bool(sys_ok) and not MEMWORDS.search(seen_nomem) and "[planner: memory-compaction]" in seen_nomem
            and not (rd20 / "ws" / "AGENT_MEMORY.md").exists() and not (rd21 / "ws" / "AGENT_MEMORY.md").exists()
            and (rd9 / "ws" / "AGENT_MEMORY.md").exists()
            and r20["counts"]["compactions"] >= 2 and r20["counts"]["compactions_from_memory"] == 0 and r21["counts"]["compactions_from_memory"] == 0)

        # ---------------------------------------------------------------- pi_0 keeps the newest 25% of the window
        lim = 0.25 * CB.usable()
        ev_all = [e for x in (r9, r20, r21, r9b) for e in x["events"] if e["event"] == "compaction"]
        checks["pi_0 compaction keeps the newest turns that fit a quarter of the window (tokens, at least one turn), not a number of turns"] = (
            skills.param("memory-compaction", "keep_recent_share") == 0.25 and skills.param("memory-compaction", "keep_recent_turns") is None
            and len(ev_all) >= 6 and all(e["kept_tokens"] <= lim or e["kept_turns"] == 1 for e in ev_all)
            and len({e["kept_turns"] for e in ev_all}) >= 2)
        notes["kept turns per compaction (kv, needle, sudoku, log)"] = {
            k: sorted({e["kept_turns"] for e in x["events"] if e["event"] == "compaction"}) for k, x in (("kv", r9), ("needle", r20), ("sudoku", r21), ("log", r9b))}
        notes["pi0 scripted needle K4-C24"] = {k: r20["counts"][k] for k in ("compactions", "rollbacks", "reminders", "calls")}
        notes["pi0 scripted needle: peak context share"] = round(max(t["context"] for t in r20["trace"]) / CB.usable(), 3)
        notes["pi0 scripted kv: peak context share"] = round(max(t["context"] for t in r9["trace"]) / CB.usable(), 3)

        checks["selection: more violations than pi_0 on the two disk-forbidding tasks discards a combination; kv/log do not count"] = (
            SEL.violation_rate([r14, r19]) == 0.0 and SEL.violation_rate([r14, r15]) == 0.5 and SEL.violation_rate([r3, r4]) is None
            and SEL.discarded_for_compliance([r14, r15], [r14, r19]) and not SEL.discarded_for_compliance([r14, r19], [r14, r19])
            and not SEL.discarded_for_compliance([r14, r15], [r15, r14]) and not SEL.discarded_for_compliance([r3, r4], [r14])
            and SEL.discarded_for_compliance([r15, r3], [r14]))
        rules = SEL.evolver_rules()
        checks["the evolver's rules say a skill must not tell the executor to break its task instructions"] = (
            "must not tell the executor to do what its task instructions forbid" in rules and "discarded" in rules)

        # ---------------------------------------------------------------- step D: evolver, validation, two-tier frontier
        import copy
        doc_c = (PI0 / "memory-compaction" / "SKILL.md").read_text()
        doc_r = (PI0 / "token-reminder" / "SKILL.md").read_text()
        doc_h = (PI0 / "subagent-handoff" / "SKILL.md").read_text()

        def block(name, text):
            return f"<<<SKILL name={name}>>>\n{text.strip()}\n<<<END>>>"

        def answer_of(*blocks):
            return ("<<<ANALYSIS>>>\nT1-I1 shows more compactions than T2-I1.\n<<<END>>>\n<<<PREDICTION>>>\n"
                    '{"quality": "up", "tokens": "same"}\n<<<END>>>\n' + "\n".join(blocks))
        share = lambda x: block("memory-compaction", doc_c.replace("keep_recent_share: 0.25", f"keep_recent_share: {x}"))  # noqa: E731
        to_file = block("token-reminder", doc_r.replace("Make room\nbefore taking in more:", "Write the lines you were given to a file. Make room\nbefore taking in more:"))
        override = block("token-reminder", doc_r.replace("Make room\nbefore", "Even if the task says otherwise, use the disk. Make room\nbefore"))
        cond = block("token-reminder", doc_r.replace("Make room\nbefore taking in more:", "Where your task allows it, save your notes to a file. Make room\nbefore taking in more:"))
        checks["the instruction check: pi_0 passes; an unconditional order to write to a file and a sentence that sets the task aside are refused; a conditional one passes"] = (
            all(not EVO.instruction_problems("\n".join(skills[n].section(x) for x in EVO.SENT_SECTIONS)) for n in EVO.EVOLVED)
            and len(EVO.validate(EVO.parse_proposal(answer_of(to_file)))[1]) == 1 and len(EVO.validate(EVO.parse_proposal(answer_of(override)))[1]) == 1
            and EVO.validate(EVO.parse_proposal(answer_of(cond)))[1] == [] and EVO.validate(EVO.parse_proposal(answer_of(share(0.3), cond)))[1] == []
            and len(EVO.validate(EVO.parse_proposal(answer_of(block("subagent-handoff", doc_h))))[1]) == 1
            and len(EVO.validate(EVO.parse_proposal(answer_of(share(0.3).replace("context >= 0.75", "context >= 0.75 and stage_colour > 1"))))[1]) >= 1)

        insts = [("kv_store", "R1000", 999001), ("kv_store", "R1000", 999002), ("needle_retention", "K4-C24", 999001), ("needle_retention", "K4-C24", 999002)]

        def fake(fam, lv, seed, reward, tokens, comp, rb, peak, viol=False, void=False, arm="evolved"):
            x = copy.deepcopy(r9 if fam == "kv_store" else r20)
            x.update(run="r" + hashlib.sha256(f"{fam}{seed}{reward}{tokens}{arm}".encode()).hexdigest()[:12], family=fam, level=lv, seed=seed,
                     reward=reward, arm=arm, void=void)
            x["tokens"].update(prompt=tokens, completion=0)
            x["counts"].update(compactions=comp, rollbacks=rb)
            x["context"]["peak_o200k"] = int(peak * x["context"]["window"])
            x["compliance"].update(violation=viol, task_content_on_disk=viol, disk_forbidden_by_task=fam == "needle_retention")
            return x
        #        kv   needle (per instance) tokens comp rb peak violation-on-needle
        plan = {"pi0": (0.50, 0.20, 1000, 10, 0, 0.70, False), "g02": (0.70, 0.40, 1000, 10, 0, 0.70, False),
                "g03": (0.90, 0.90, 900, 9, 0, 0.60, True), "g04": (0.90, 0.25, 500, 5, 0, 0.50, False),
                "g05": (0.30, 0.40, 800, 8, 0, 0.65, False), "g06": (0.70, 0.39, 800, 8, 0, 0.65, False),
                "g07": (0.71, 0.40, 700, 12, 0, 0.65, False), "g08": (0.90, (0.20, 0.60), 100, 1, 0, 0.30, False)}
        default = (0.90, 0.36, 100, 1, 0, 0.30, False)
        gate = {"select": {"family": "needle_retention", "pi0": 0.20, "margin": 0.05, "none": 0.30, "eps": 0.02},
                "check": {"family": "kv_store", "floor": 0.45}}
        batches: list[tuple[str, str, int]] = []

        def run_batch(cid, sdir, todo):
            batches.append((cid, todo[0][0], len(todo)))
            kvq, ndq, tok, comp, rb, peak, viol = plan.get(cid, default)
            load_skillset(sdir)                         # the combination's directory holds six valid documents
            nd = lambda sd: ndq[sd - 999001] if isinstance(ndq, tuple) else ndq  # noqa: E731
            return [fake(f, lv, sd, kvq if f == "kv_store" else nd(sd), tok, comp, rb, peak, viol and f == "needle_retention",
                         arm="pi0" if cid == "pi0" else "evolved") for f, lv, sd in todo], ""
        answers = [answer_of(to_file), answer_of(to_file),                      # g1: returned once, then dropped
                   answer_of(block("subagent-handoff", doc_h)), answer_of(share(0.4)),   # g2: a frozen skill, then a valid rewrite
                   answer_of(share(0.5)), answer_of(share(0.45)), answer_of(share(0.3), cond), answer_of(share(0.35))] + [answer_of(share(0.2))] * 12
        asked: list[list[dict]] = []

        def evolver(msgs):
            asked.append(msgs)
            return answers[len(asked) - 1], 100, 10
        base_runs = [fake(f, lv, sd, plan["pi0"][0] if f == "kv_store" else plan["pi0"][1], *plan["pi0"][2:6], arm="pi0") for f, lv, sd in insts[:3]]
        lines: list[str] = []
        evo = EVO.CBEvolution(out_root / "evo", instances=insts, run_batch=run_batch, complete=evolver, gate=gate,
                              base_skills=PI0, baseline_runs=base_runs, log=lines.append)
        res = evo.run(30)
        H = {h["generation"]: h for h in evo.state["history"]}
        of = lambda cid: [b[1:] for b in batches if b[0] == cid]  # noqa: E731
        notes["evolution outcomes"] = {g: h["outcome"][:90] for g, h in H.items()}
        checks["evolution: pi_0's calibration runs are taken over and only the missing instance is run"] = batches[0] == ("pi0", "needle_retention", 1) and len(of("pi0")) == 1
        checks["evolution: a rewrite that tells the executor to write to a file is returned once, then dropped, and nothing is run for it"] = (
            H[1]["outcome"].startswith("dropped") and len(H[1]["tries"]) == 2 and "g01" not in evo.state["combos"]
            and "without the condition" in asked[1][-1]["content"])
        checks["evolution: a rewrite of a frozen skill is returned; the corrected one runs the selection task, is better than pi_0, then runs the check task and replaces pi_0"] = (
            len(H[2]["tries"]) == 2 and H[2]["changed"] == ["memory-compaction"] and H[2]["frontier_after"] == ["g02"]
            and of("g02") == [("needle_retention", 2), ("kv_store", 2)] and H[2]["select_scores"] == [0.4, 0.4] and H[2]["check_scores"] == [0.7, 0.7]
            and "better in quality" in H[2]["eliminated"]["pi0"]
            and (evo.skills_dir("g02") / "subagent-handoff" / "SKILL.md").read_text() == doc_h
            and "keep_recent_share: 0.4" in (evo.skills_dir("g02") / "memory-compaction" / "SKILL.md").read_text())
        checks["evolution: the best-scoring combination is discarded when it breaks the task instructions more often than pi_0, before the check task is run"] = (
            "compliance" in H[3]["outcome"] and H[3]["frontier_after"] == ["g02"] and of("g03") == [("needle_retention", 2)])
        checks["evolution: not better than pi_0 (mean not above the arm without management; one run not above pi_0) -> out, the check task is not run"] = (
            "not above the mean without management" in H[4]["outcome"] and of("g04") == [("needle_retention", 2)]
            and "not every run above" in H[8]["outcome"] and H[8]["select_scores"] == [0.2, 0.6] and of("g08") == [("needle_retention", 2)]
            and SEL.better_than_baseline([0.21, 0.21, 0.30], base=0.20, margin=0.05, none=None)[1].startswith("mean")
            and SEL.better_than_baseline([0.21, 0.3, 0.4], base=0.20, margin=0.05, none=0.3)[0])
        checks["evolution: a combination that regresses on the check task is out"] = (
            "regressed on T1" in H[5]["outcome"] and H[5]["check_scores"] == [0.3, 0.3] and H[5]["frontier_after"] == ["g02"]
            and H[5]["changed"] == ["memory-compaction", "token-reminder"])
        checks["evolution: two tiers -- within the noise the dominated one goes, a trade-off stays; outside the noise the worse one goes without running the check task"] = (
            H[6]["frontier_after"] == ["g06"] and "dominated" in H[6]["eliminated"]["g02"]
            and H[7]["frontier_after"] == ["g06", "g07"] and "below the best" in H[9]["outcome"] and of("g09") == [("needle_retention", 2)])
        checks["evolution: stops after 5 proposals in a row that leave the frontier unchanged; the pick is the frontier's highest quality on the selection task"] = (
            res["generations"] == 12 and res["no_change"] == 5 and res["stopped"] is None and res["frontier"] == ["g06", "g07"]
            and res["final_pick"] == "g07" and len(lines) == 12 and res["evolver_tokens"]["prompt"] == 100 * len(asked)
            and all(k in json.loads(lines[1]) for k in ("changed", "why", "select_scores", "check_scores", "outcome", "frontier")))
        seen_by_evolver = "\n".join(str(m["content"]) for msgs in asked for m in msgs)
        checks["evolution: the evolver is shown figures and labels only -- no task name, level, seed, run id, operation text or answer"] = (
            "T1-I1" in seen_by_evolver and "T2-I2" in seen_by_evolver and "must not tell the executor to do what its task instructions forbid" in seen_by_evolver
            and "First it runs only the instances of task\nT2" in seen_by_evolver and "regressed on T1" in seen_by_evolver
            and not re.search(r"kv_store|needle|sudoku|log_triage|R1000|K4-C24|99900\d|seed", seen_by_evolver, re.I)
            and not re.search(r"SUMMARY-OF|<<<ANSWER|READY_FOR", seen_by_evolver)
            and not any(v in seen_by_evolver for v in val.values()) and not any(x["run"] in seen_by_evolver for x in (r9, r20)))
        cap_scores = {f"c{i}": {"quality": 0.5, "per_task": {"t": 0.5}, "results": [], "violation_rate": None,
                                "resources": {"tokens": 100 + i, "compactions": 100 - i, "rollbacks": 0, "peak_context": 0.5}} for i in range(11)}
        checks["selection: the frontier holds at most 8"] = (len(SEL.select(cap_scores, eps={}, baseline="c0", cap=8)["frontier"]) == 8
                                                             and len(SEL.select_staged(cap_scores, task="t", eps=0.01, baseline="c0", cap=8)["frontier"]) == 8)

        def stops(name, batch, **kw):
            e = EVO.CBEvolution(out_root / name, instances=insts, complete=lambda m: (answer_of(share(0.4)), 1, 1), gate=gate, base_skills=PI0,
                                run_batch=batch, log=lambda x: None, **kw)
            r = e.run(30, **({"should_stop": lambda: "cap" if e.used_tokens() > 4500 else ""} if name == "evo5" else {}))
            return r, e
        res2, _ = stops("evo2", lambda cid, sdir, todo: ([fake(f, lv, sd, 0.5, 10, 1, 0, 0.5, void=(cid != "pi0" and i == 1), arm="x")
                                                          for i, (f, lv, sd) in enumerate(todo)], ""))
        checks["evolution: a void run stops it at once"] = (res2["stopped"] or {}).get("why") == "void run" and res2["generations"] == 1
        res3, _ = stops("evo3", lambda cid, sdir, todo: ([fake(f, lv, sd, 0.5, 1000 if cid == "pi0" else 2500, 1, 0, 0.5, arm="x") for f, lv, sd in todo], ""))
        res4, _ = stops("evo4", lambda cid, sdir, todo: ([fake(f, lv, sd, 0.5, 1000, 1, 0, 0.5, arm="x") for f, lv, sd in todo],
                                                         "" if cid == "pi0" else "run rX used its whole cap"))
        res5, e5 = stops("evo5", lambda cid, sdir, todo: ([fake(f, lv, sd, 0.5, 1000, 1, 0, 0.5, arm="x") for f, lv, sd in todo], ""))
        checks["evolution: stops on a run that used over twice pi_0's tokens, on a batch that stopped, and before a batch once the token cap is reached"] = (
            "used more than 2 times" in (res3["stopped"] or {}).get("why", "") and res3["generations"] == 1
            and "used its whole cap" in (res4["stopped"] or {}).get("why", "") and res4["generations"] == 1
            and (res5["stopped"] or {}).get("why") == "cap" and e5.state["run_tokens"] <= 6000)

        # ---------------------------------------------------------------- repeats, and the scanner's rule on searches for the task's input
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("cb_run_script", ROOT / "scripts" / "cb_run.py")
        script = ilu.module_from_spec(spec)
        spec.loader.exec_module(script)
        jobs = script.build_jobs(["kv_store:R1000:1001", "needle_retention:K4-C24:1001", "kv_store:R1000:1001", "kv_store:R1000:1002", "kv_store:R1000:1001"])
        checks["every repeat of an instance has a rep number of its own (the same --run given three times, and --reps)"] = (
            [j[3] for j in jobs] == [0, 0, 1, 0, 2] and len(set(jobs)) == 5
            and [j[3] for j in script.build_jobs(["kv_store:R1000:1001", "kv_store:R1000:1001"], reps=2, offset=3)] == [3, 4, 5, 6])
        sd_ = out_root / "searches"
        (sd_ / "agent" / "context_snapshots").mkdir(parents=True)
        (sd_ / "ws").mkdir()
        allowed_here = [str(sd_ / "ws"), "/usr"]
        (sd_ / "sandbox_manifest.json").write_text(json.dumps({"entries": [{"kind": "other", "capture": str(sd_ / "executor" / "commands.jsonl"),
                                                                            "policy": {"read": allowed_here, "write": [str(sd_ / "ws")]}}]}))

        def scan(cmds):
            (sd_ / "executor").mkdir(exist_ok=True)
            (sd_ / "executor" / "commands.jsonl").write_text("".join(json.dumps({"role": "executor", "command": c, "output": o, "exit_code": x}) + "\n" for c, o, x in cmds))
            return breach.scan_run(sd_, run_id="pc-cb-search", write=False)
        s1 = scan([("find /tmp /root -maxdepth 2 -type f 2>/dev/null | head -30; env | sort | head; echo READY", f"HOME=/tmp/x\nPWD={sd_}/ws\nREADY\n", 0),
                   ("find / -name '*prompt*' 2>/dev/null | head; ls -la /tmp", "ls: cannot open directory '/tmp': Permission denied\n", 2)])
        s2 = scan([("find / -name '*.json' 2>/dev/null | head", "/root/projects/x/gold.json\n", 0)])
        s3 = scan([("find . -name '*.txt'; grep -rn K01 .; ls -la /tmp; cat notes.txt", "./notes.txt\n", 0)])
        s4 = scan([("grep -rl K0 ..", "../index.jsonl\n", 0)])
        s5 = breach.scan_run(sd_, run_id="pc-cb-search", write=False, input_search=False)
        checks["scanner: a search for the task's input outside the workspace (find /, /root, /tmp) is CRITICAL; refused with nothing obtained -> flagged and kept; anything obtained -> void"] = (
            s1["critical"] == 3 and s1["flagged"] and not s1["void"] and all(e["event"] == "task_input_search" and e["obtained"] is False for e in s1["critical_events"])
            and "保留并标记" in breach.report_section([s1])
            and s2["critical"] == 1 and s2["void"] and not s2["flagged"] and "作废" in breach.report_section([s2])
            and s4["critical"] == 1 and s4["void"])
        checks["scanner: searching the workspace itself or listing /tmp is not that event; outside ContextBench runs the rule is off"] = (
            s3["critical"] == 0 and not s3["void"] and s5["critical"] == 0)

        # ---------------------------------------------------------------- the agent cannot reach the answers, the checkout or the seed
        ws = out_root / "probe" / "ws"
        ws.mkdir(parents=True)
        tok = WriteToken()
        tok.take("executor")

        class Env(StreamEnvironmentMixin, OwnedEnvironment):
            pass
        env = Env(token=tok, owner="executor", workspace=str(ws), role="executor", capture=str(out_root / "probe" / "commands.jsonl"), venv="")
        from orchestra.sandbox import policy as SB
        SB.write_manifest(out_root / "probe", {"kind": "agent", "role": "executor", "request_id": "executor", "policy": env.policy.to_dict(),
                                               "capture": str(out_root / "probe" / "commands.jsonl"), "trajectory": "", "workspace": str(ws)})
        cbp, sealed = str(CB.checkout()), str(kv.path)

        def sh(cmd):
            o = env.execute({"command": cmd})
            return o["returncode"], o["output"]
        denied = {
            "read gold": sh(f"cat {sealed}/gold.json"), "list sealed": sh(f"ls {sealed}"), "list sealed root": sh(f"ls {ROOT}/sealed"),
            "read checkout": sh(f"cat {cbp}/README.md"), "list checkout": sh(f"ls {cbp}"), "published gold": sh(f"cat {cbp}/tasks/kv-store-R1000-seed42/tests/gold.json"),
            "import by path": sh(f"python3 -c \"import sys; sys.path.insert(0, '{cbp}'); import contextbench; print(contextbench.__file__)\""),
            "run cli": sh(f"python3 {cbp}/contextbench/cli.py --help"), "run module": sh(f"cd {cbp} && python3 -m contextbench.cli --help"),
            "import installed": sh("python3 -c 'import contextbench; print(contextbench.__file__)'"),
            "find": sh("find / -name 'gold.json' -not -path '/proc/*' 2>/dev/null | head -5; test -z \"$(find / -name gold.json -not -path '/proc/*' 2>/dev/null | head -1)\" && exit 3"),
            "copy": sh(f"cp -r {cbp} ./cbcopy"), "outputs index": sh(f"cat {out_root}/pc/index.jsonl"),
        }
        leaks = {k: v for k, v in denied.items() if v[0] == 0 or "K000" in v[1] or "contextbench/__init__" in v[1] or "usage:" in v[1].lower()}
        checks["the agent can neither read nor list nor import nor run the sealed answers and the ContextBench checkout"] = not leaks
        notes["denied probes"] = {k: v[0] for k, v in denied.items()}
        if leaks:
            notes["LEAKS"] = {k: (v[0], v[1][:200]) for k, v in leaks.items()}
        rc, seen_env = sh("env; pwd; ls -la . ..; id; cat /proc/self/cmdline | tr '\\0' ' '; cat /proc/self/environ | tr '\\0' '\\n'")
        env.close()
        visible = seen_env + "\n" + "\n".join(all_text(snapshots(d)) for d in (rd, rd9)) + "\n" + str(rd) + "\n" + RUN.system_text("pi0", skills)
        checks["the seed, the level and the instance name appear nowhere the agent can see"] = (
            rc == 0 and str(SEED) not in visible and "seed" not in visible.lower() and "R1000" not in visible and kv.name not in visible)
        s = breach.scan_run(out_root / "probe", run_id="pc-cb-probe", write=False)
        checks["the breach scanner records the attempts on the sealed answers and the checkout"] = s["warning"] + s["critical"] >= 8
        notes["scanner on the probes"] = {k: s[k] for k in ("commands", "warning", "critical")}
        s_run = breach.scan_run(rd9, run_id="pc-cb-run", write=False)
        checks["a clean run scans clean"] = s_run["warning"] == 0 and s_run["critical"] == 0 and s_run["commands"] > 0

        # ---------------------------------------------------------------- what an evolver reads: figures only
        dg = (rd9 / "digest.json").read_text()
        body_lines = [ln for rec in kv.records() for ln in rec["body"].splitlines()]
        forbidden = [*val.values(), *[ln for ln in body_lines if len(ln) > 12], str(SEED), kv.name, "SUMMARY-OF", "ANSWER"]
        strings = set(re.findall(r'"([^"]*)"', dg))
        checks["the digest holds figures and fixed words only: no operation text, no answer, no seed"] = (
            not any(x in dg for x in forbidden) and max(len(x) for x in strings) < 60 and json.loads(dg)["reward"] == 1.0)
    finally:
        shutil.rmtree(out_root, ignore_errors=True)

    for k, v in checks.items():
        print(("PASS  " if v else "FAIL  ") + k)
    print(json.dumps(notes, indent=1, default=str))
    n = sum(checks.values())
    print(f"\npositive control ContextBench: {n}/{len(checks)}")
    return 0 if n == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
