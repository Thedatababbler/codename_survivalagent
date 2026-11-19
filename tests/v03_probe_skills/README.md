# "Working carefully" probe skills (measurement only, user decision 2026-11-19)

Not a candidate, never compared with anything. pi_0 with one change: at every stage declaration (the last one is the
submission) `completion-check` asks in turn for passing tests of the stage's work (`require_selftest`), one review
(`require_review`), and a code change after the review (`change_after_review: true`), then accepts. A requirement is
dropped after 4 refusals. The other five documents are copies of `memory/planner/skills/`.

    uv run python scripts/v03_run_round.py --task <task> --round probe-1 --tokens 3000000 --window 64000 \
        --skills tests/v03_probe_skills --skills-id probe --reviews 8 --subagents 16
