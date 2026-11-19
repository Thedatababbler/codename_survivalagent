---
name: token-reminder
description: Harness test. Reminds almost at once.
triggers:
  - when: stage_tokens >= 0.02
    action: remind
params:
  advise: either
---
## Mechanism
Harness test: the reminder fires as soon as a small share of the stage's tokens is used.

## What to do
This is only a note that the reminder works: carry on with the stage as planned.

## Reading the readout
`stage tokens` is the share of this stage's allocation already used; `round tokens` is the share of the whole run.

## Checking it worked
The note arrived once; nothing else is expected.
