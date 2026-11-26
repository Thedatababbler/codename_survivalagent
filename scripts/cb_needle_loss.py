#!/usr/bin/env python3
"""Where the marked lines of needle_retention runs were lost (a diagnostic for the developers; never an evolver's
input). No quota: reads the snapshots of finished runs.

    uv run python scripts/cb_needle_loss.py outputs/cb/calib-2

A line counts as present in a snapshot when it is verbatim in any message. For every line absent from the final
snapshot, the snapshot at which it disappeared for good is classified by where the line last stood:
  first_summary   in the raw turns (never yet summarised) -> replaced by a summary that does not hold it
  later_summary   only inside a summary note               -> the next summary did not carry it over
  not_compaction  it disappeared at a step that was not a compaction
  never_seen      it is in no snapshot (its piece was not delivered)
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

NEEDLE = re.compile(r"^\[n\d+i\d+#[0-9a-f]+\].*$", re.M)
NOTE = "[Context compacted"


def analyse(run_dir: Path) -> dict:
    snaps = [json.loads(f.read_text()) for f in sorted((run_dir / "agent" / "context_snapshots").glob("*.json"))]
    def text(m):
        return str(m.get("content") or "") + "".join(str((tc.get("function") or {}).get("arguments", "")) for tc in m.get("tool_calls") or [])
    given = set()                                        # the lines as the stream delivered them
    for s in snaps:
        for m in s["messages"][2:]:
            if m.get("role") == "user" and not text(m).startswith(NOTE):
                given |= set(NEEDLE.findall(text(m)))
    raw, noted, notes = [], [], []
    for s in snaps:
        r, n, nt = set(), set(), ""
        for m in s["messages"][2:]:
            c = text(m)
            hit = {x for x in given if x in c}
            if c.startswith(NOTE):
                n |= hit
                nt = c
            else:
                r |= hit
        raw.append(r), noted.append(n), notes.append(nt)
    allof = given
    final = raw[-1] | noted[-1]
    out = collections.Counter(kept=len(final))
    for x in allof - final:
        last = max(i for i in range(len(snaps)) if x in raw[i] or x in noted[i])
        if last + 1 >= len(snaps) or notes[last + 1] == notes[last]:
            out["not_compaction"] += 1
        elif x in raw[last]:
            out["first_summary"] += 1
        else:
            out["later_summary"] += 1
    out["seen"] = len(allof)
    return dict(out)


def main() -> int:
    tot: dict[str, collections.Counter] = {}
    for rd in sorted(Path(sys.argv[1]).glob("r*")):
        if not (rd / "result.json").is_file():
            continue
        res = json.loads((rd / "result.json").read_text())
        if res["family"] != "needle_retention":
            continue
        a = analyse(rd)
        k, c = re.match(r"K(\d+)-C(\d+)", res["level"]).groups()
        n = int(k) * int(c)
        a["never_seen"] = max(0, n - a["seen"])
        print(json.dumps({"run": rd.name, "level": res["level"], "seed": res["seed"], "rep": res["rep"], "reward": round(res["reward"], 3),
                          "total": n, **a}))
        tot.setdefault(res["level"], collections.Counter()).update({**a, "total": n})
    for lv, c in tot.items():
        lost = c["total"] - c["kept"]
        print(json.dumps({"level": lv, "total": c["total"], "kept": c["kept"], "lost": lost,
                          **{k: f"{c[k]} ({c[k] / lost:.0%} of lost)" for k in ("first_summary", "later_summary", "not_compaction", "never_seen")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
