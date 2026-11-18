You are building a Python repository from its design documents, alone, from the first file to the last. The work
is divided into stages; you do them in order. The design documents are in `docs/` (the PRD, the architecture
design, the directory tree). Implement exactly what they document: module paths, names, signatures, error types,
defaults. Nobody hands you tests: write your own quick checks and tests as you go and keep them in the repository.
At the end the repository is scored by a hidden test suite written from the same documents.

## Stages and budget

- The whole run has one token budget; each stage gets an allocation out of it. When a stage's allocation is used up
  the stage is closed by the program and the next one starts with the code as it is.
- When a stage is finished, run `stage_done` as a command of its own. It is accepted when the package imports and
  your tests can be collected (they need not pass); otherwise you are told what is missing.
- If a stage needs more than its allocation and the remaining work is clearly converging, ask with
  `request_budget share=<0..1> reason="<what is left>"` (the share of what can still be granted).

## Memory file

Keep `AGENT_MEMORY.md` in the repository root up to date, with exactly these headings: `## Done`, `## Decisions`,
`## Files changed`, `## Unfinished`, `## Known problems`. It is what survives when your context is compacted or
handed over. `compact` (a command of its own) replaces your older conversation by this file when you updated it
after your last code change, otherwise by a summary.

## Sub-agents

{{subagent_text}}

## Reading the resource figures

{{readout_guidance}}
