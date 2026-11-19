# AdaMAS-unit experiment log

## 2026-11-09 — AdaMAS's public-symbol rule misses multi-line import blocks (affects AdaMAS ceiling analyses)

- **Found:** on nl2_deepdiff, the design document lists its public API in the parenthesised, multi-line form
  `from deepdiff.x import (\n a, b,\n)`. AdaMAS's `public_symbols.derive_public_symbols` (34276d69, copied unchanged
  here) reads only single-line `from x import a, b`, so it attributes 20 names where the documents state 115.
- **Effect measured (sealed, numbers only):** of the 68 held-out feedback-half files that collect nothing because an
  imported name is missing (10 rounds), AdaMAS's rule puts all 68 in "the documents do not name it". The import-check
  manifest (`src/adamas_unit/importcheck.py`, which reads that form) puts 65 in "named with a module" (60 imported
  from exactly that module), 0 in "named without a module" and 3 in "not named".
- **Why it matters later:** analyses on AdaMAS that used this rule to split held-out misses into "the documents did
  not ask for it" against "asked for it but not done" (ceiling or recall analyses, e.g. `scripts/sealed/symbol_recall.py`,
  the author audit's symbol coverage) may have **under-counted "documented but not done right"** on any task whose
  documents use multi-line imports. Re-check those conclusions before relying on them. AdaMAS itself was not
  changed from here (other sessions own it).

## 2026-11-18 — v0.3 forced-trigger smoke `forced-1`: void; the sandbox does not stop `chmod`

- **Run:** `outputs/smoke_v03/forced-1/nl2_python-pathspec` (test skills `tests/v03_forced_skills`, window 12000,
  first stage only, cap 500,000). Used 317,244 tokens. **Void** (3 CRITICAL), nothing in it is evidence of anything.
- **Cause (implementing agent's error):** the round directory was passed as a relative path, so the repository
  path in the sandbox policy matched nothing and no agent could open its own repository. The executor and three
  sub-agents spent the run diagnosing permissions.
- **Sandbox hole found:** Landlock does not mediate `chmod`/`chown`. The executor ran
  `chmod 777 /root /root/projects /root/projects/survival …/outputs …/ws` and it succeeded. Nothing outside the
  repository was read or listed (only permission bits and owners, via `ls -ld`, `stat`, `getfacl`, `namei`). The
  scanner did not flag the `chmod` itself. The same sandbox code runs in AdaMAS and AdaMAS-single (not changed
  from here; the user synchronises them).
- **Permissions, as changed and restored:**
  - `/root`: **755 is the value the user set by hand.** The run's `chmod` made it 777; the user set it to 755. On
    2026-11-19 the implementing agent wrongly reset it to 700 (taking the value seen at the start of the run for
    the original) and wrote in its report that the 755 was "of unknown origin": that statement was wrong and is
    corrected here. Set back to 755 on 2026-11-19. `/root`'s permissions are not to be changed again.
  - `/root/projects`, `/root/projects/survival`, `outputs`, `outputs/smoke_v03`, `forced-1`,
    `forced-1/nl2_python-pathspec`, `…/ws`: 777 by the run, set back to 775 (their value before the run).
    `…/ws/AGENT_MEMORY.md`: 777 by the run, set to 664.
- **Fixes not touching sandbox rules:** `Round` makes its directory absolute; management commands are written to
  the command capture (the scanner's `capture_empty` on them was a false alarm). Positive control 38/38.
- **Decided by the user (2026-11-19):** agents run as an ordinary user that owns only the round's repository; the
  scanner flags `chmod`/`chown`/`chgrp`/`setfacl` outside the repository as CRITICAL; a start-up check that each
  agent can read and write its repository; a new sandbox rule in `CLAUDE.md`.
- **Done 2026-11-19 (before any further run):** the launcher drops to `sandbox.agent_uid` (61234, owns nothing
  else, no passwd entry) after the Landlock rules are in force and refuses to run if it cannot; `policy.grant`
  hands the run's repository and private tmp to that user; `ConfinedEnvironment.preflight` checks read/write
  (reviewer: read, no write) before the first command; scanner event `permission_change` (CRITICAL); prompts of
  every agent (v0.3 and v0.2 `common.md`) say to stop and report; `CLAUDE.md` sandbox rule 7. Sealed scoring
  (file rules off) still runs as the driver's user. Positive controls: v0.3 46/46, v0.2 81/81, sandbox self-check
  11/11 (its AdaMAS-source probe now passes: the directory is refused to the agent user).
