# Calibration skills (user decision 2026-11-19)

pi_0 with one change: `completion-check` prompts the executor to continue when it declares the LAST stage done
(`stage_progress >= 1.0` -> `prompt_continue`), until two prompts in a row change no code (`idle_prompts_to_accept: 2`)
or the round's budget is used up. The other five documents are copies of `memory/planner/skills/`. Used only to
measure the unconstrained usage of the evolution tasks (evolution budget = that usage x 0.6); never a candidate.

    uv run python scripts/v03_run_round.py --task <task> --round calib-2 --tokens 3000000 --window 64000 \
        --skills configs/v03_calibration_skills --skills-id pi0+continue
