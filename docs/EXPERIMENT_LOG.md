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
