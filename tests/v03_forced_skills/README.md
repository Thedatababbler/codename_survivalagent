# Forced-trigger skills (harness test only)

Not a candidate, never compared with anything. Thresholds are set very low so that, together with a small context
window (`--window 12000`), one short run exercises every rewrite of the message list with the real model: a
roll-back, a compaction (asked and by the framework), a review, a repair and a worker handoff.

    uv run python scripts/v03_run_round.py --task nl2_python-pathspec --round forced-1 --tokens 500000 \
        --skills tests/v03_forced_skills --skills-id forced --window 12000 --first-stage-only --compressions 30 \
        --out-root outputs/smoke_v03 --no-score
    uv run python scripts/v03_smoke_report.py outputs/smoke_v03/forced-1/nl2_python-pathspec
