#!/usr/bin/env python3
"""Replay summary calls on saved compaction moments (user decision 2026-11-26). SPENDS QUOTA (summary calls only).

    set -a; . ./.env; set +a
    uv run python scripts/cb_summary_replay.py --round calib-1 --out outputs/cb/summary-replay-1 --per-task 20

No agent runs and no task is rerun: for ``--per-task`` compaction moments of each task (spread evenly over the
round's pi_0 runs), the turns that the compaction replaced are rebuilt from the ``turn`` snapshot written right
before it (the same cut as ``StreamAgent.compact_keep``: the newest turns within ``keep_recent_share`` of the window
stay) and sent through ``cb_run.make_summarizer`` with pi_0's summary instruction. Prints the share of replies that
pass ``cb_run.summary_problem`` (first try; after the one retry) and writes every reply to ``<out>/replies.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adamas_unit.model import complete, load_env  # noqa: E402
from survival import cb as CB  # noqa: E402
from survival import cb_run as RUN  # noqa: E402
from survival.skills import load_skillset  # noqa: E402

NEEDLE = re.compile(r"^\[n\d+i\d+#[0-9a-f]+\].*$", re.M)


def moments(run_dir: Path, share: float) -> list[dict]:
    """One entry per ``turn`` snapshot that precedes a compaction: the task text and the replaced turns."""
    snaps = [json.loads(f.read_text()) for f in sorted((run_dir / "agent" / "context_snapshots").glob("*.json"))]
    out = []
    for i, s in enumerate(snaps):
        nxt = snaps[i + 1]["messages"] if i + 1 < len(snaps) else []
        if s["kind"] != "turn" or len(nxt) < 3 or not str(nxt[2].get("content") or "").startswith("[Context compacted"):
            continue
        ms = s["messages"]
        starts = [j for j, m in enumerate(ms) if m.get("role") == "assistant" and j >= 2]
        keep = 0
        for k in range(1, len(starts) + 1):
            if CB.count(ms[starts[-k]:]) > share * CB.usable():
                break
            keep = k
        keep = max(keep, 1)
        if len(starts) <= keep:
            continue
        out.append({"run": run_dir.name, "snapshot": i, "task": str(ms[1]["content"]), "replaced": ms[2:starts[-keep]],
                    "was": str(nxt[2]["content"]).split("\n", 1)[-1]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-task", type=int, default=20)
    ap.add_argument("--parallel", type=int, default=4)
    a = ap.parse_args()
    load_env()
    CB.ensure()
    skills = load_skillset(ROOT / "configs" / "cb_skills_pi0")
    prompt = RUN.summary_instruction(skills)
    share = float(skills.param("memory-compaction", "keep_recent_share"))
    by_task: dict[str, list[dict]] = {}
    for rd in sorted((RUN.OUT / a.round).glob("r*")):
        if not (rd / "result.json").is_file():
            continue
        res = json.loads((rd / "result.json").read_text())
        if res["arm"] != "pi0":
            continue
        ms = moments(rd, share)
        n = max(1, a.per_task // 5)
        pick = ms if len(ms) <= n else [ms[round(j * (len(ms) - 1) / (n - 1))] for j in range(n)]
        by_task.setdefault(res["family"], []).extend(pick)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = out / "replies.jsonl"
    rows = []

    def one(fam: str, m: dict) -> dict:
        stats: dict[str, int] = {}
        tries: list[dict] = []

        def call(msgs):
            r = complete(msgs)
            tries.append({"reply": r[0], "problem": RUN.summary_problem(r[0]), "prompt_tokens": r[1], "completion_tokens": r[2]})
            return r
        text, p, c = RUN.make_summarizer(call, m["task"], prompt, stats=stats)(m["replaced"])
        row = {"family": fam, "run": m["run"], "snapshot": m["snapshot"], "replaced_messages": len(m["replaced"]),
               "first_ok": not tries[0]["problem"], "final_ok": not tries[-1]["problem"], "tries": tries,
               "prompt_tokens": p, "completion_tokens": c, "was": m["was"], "was_ok": not RUN.summary_problem(m["was"])}
        if fam == "needle_retention":
            need = set(NEEDLE.findall("\n".join(str(x.get("content") or "") for x in m["replaced"])))
            row["needles_in_replaced"] = len(need)
            row["needles_in_summary"] = sum(1 for x in need if x in text)
        return row

    jobs = [(fam, m) for fam, ms in by_task.items() for m in ms[:a.per_task]]
    with ThreadPoolExecutor(max_workers=a.parallel) as ex:
        for row in ex.map(lambda j: one(*j), jobs):
            rows.append(row)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
    for fam in by_task:
        rs = [r for r in rows if r["family"] == fam]
        line = {"task": fam, "calls": len(rs), "ok_first_try": sum(r["first_ok"] for r in rs), "ok_after_retry": sum(r["final_ok"] for r in rs),
                "old_reply_ok": sum(r["was_ok"] for r in rs), "prompt_tokens": sum(r["prompt_tokens"] for r in rs),
                "completion_tokens": sum(r["completion_tokens"] for r in rs)}
        if fam == "needle_retention":
            line["needles_kept"] = f"{sum(r['needles_in_summary'] for r in rs)}/{sum(r['needles_in_replaced'] for r in rs)}"
        print(json.dumps(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
