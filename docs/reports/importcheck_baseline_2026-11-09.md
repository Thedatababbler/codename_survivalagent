# Import-check patch, step 3: tier50 π_0 × 3 on deepdiff (2026-11-09)

Code cb3d7c8 (unattributed names recorded only; one fix per round before the final submission; fix caps 150k tokens /
600 s / 150 lines). Manifest sha256 946f7ef1…021c7e. Rounds tier50-ic-r1..r3, B = 1,586,206 tokens, set adamas2.
Held-out: feedback half only, sealed programs (numbers only).

## Sandbox breach warnings

- Runs: 3; commands 88 / 108 / 107.
- WARNING: 4 (read_denied, writes to /tmp by builders/repairers, all blocked): r1 1, r2 1, r3 2.
- CRITICAL: 0. Nothing voided.

## Results

| round | fix | before → after problems | fix tokens / s / calls / lines | ratio before | ratio after | collect after | pass-after-collect after | dead files after |
|---|---|---|---|---|---|---|---|---|
| r1 | undone (a `try: import numpy … except ImportError` block is not a bare import) | 22 → 22 | 81,612 / 78 / 7 / 60 | 0.086 | 0.086 | 0.323 | 0.265 | 11 |
| r2 | undone **by a checker bug** (a function defined twice in one module) | 13 → 13 | 82,581 / 71 / 9 / 25 | 0.165 | 0.165 | 0.505 | 0.327 | 6 |
| r3 | kept | 32 → 0 | 100,834 / 80 / 7 / 49 | 0.125 | 0.294 | 0.848 | 0.346 | 2 |

Before the fix in r3: collect 0.337, pass-after-collect 0.373, 9 dead files.

| set | values | range | std | mean |
|---|---|---|---|---|
| tier50 before the patch (r3/r4/r5, reference only) | 0.086 / 0.281 / 0.135 | 0.195 | 0.083 | 0.167 |
| this patch, before the fix | 0.086 / 0.165 / 0.125 | 0.079 | 0.032 | 0.125 |
| this patch, after the fix (formal) | 0.086 / 0.165 / 0.294 | 0.208 | 0.086 | 0.182 |
| reference only: r1, r2 replayed with the fixed checker and the guarded import allowed; r3 as run | 0.251 / 0.317 / 0.294 | 0.066 | 0.027 | 0.287 |

Replay: the fix sessions' recorded commands re-executed in the sandbox on a copy of the pre-fix code (no model call).
With the duplicate-definition bug fixed, r2's fix passes the scope check (25 lines, 0 problems left); r1's fails only
on the guarded import (0 problems left). These numbers are not a run under the rules and are not used for any decision.

Unit-end checks (recorded only), missing names at the end of U1..U5: r1 9/14/14/17/17, r2 3/9/9/9/9, r3 2/8/12/15/26.

## Reading

- Where the fix is kept, the collect rate goes to 0.85–0.87 in all three cases (one formal, two replayed), and the
  remaining spread is in pass-after-collect (0.30–0.36): range 0.066, std 0.027 against 0.195 / 0.083 before the patch.
- The formal result does not show that, because two of three fixes were undone, one by a bug of mine.
- Bug fixed after the runs (scope check keyed definitions by name; positive control 77/77 with a new duplicate case);
  not committed yet.
