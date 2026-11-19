---
name: token-reminder
description: Baseline. One reminder when the stage's tokens are nearly used up.
triggers:
  - when: "stage_tokens >= 0.90"
    action: remind
params:
  advise: either
  repeat: false
---
## Mechanism
Every stage has a token allocation. When it is used up the stage is closed by the program and the next stage starts
with the code as it is. This skill sends the executor one reminder shortly before that point. It does not change any
allocation.

## What to do
This stage's tokens are nearly used up. Either bring the stage to a close now (make sure the package imports and
your tests can be collected, then declare the stage done), or, if the remaining work is small and clearly
converging, ask for more with a budget request and say what is left.

## Reading the readout
`stage tokens` is the share of this stage's allocation already used; `round tokens` is the share of the whole
round. A stage that reaches its allocation is closed whether or not it was declared done.

## Checking it worked
After the reminder the stage should end by a declaration, not by running out. A stage closed by the program with
unfinished work, or a budget request made with nothing left to ask from, means the reminder came too late or was
ignored.
