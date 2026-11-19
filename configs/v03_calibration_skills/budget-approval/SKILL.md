---
name: budget-approval
description: Baseline. Every top-up request is approved up to the mechanical cap.
triggers: []
params:
  mode: approve_within_cap
  max_grant_share: 1.0
---
## Mechanism
When a stage needs more tokens than its allocation, the executor may ask for a top-up. The program cuts any grant
to the mechanical cap: what the round has left, minus the floor kept for every later stage, minus what the stage
still holds. This baseline grants what is asked, up to that cap.

## What to do
The request was decided; the answer says how much was granted. Use the grant for the work named in the request,
then declare the stage done.

## Reading the readout
After a grant `stage tokens` drops, because the allocation grew; `round tokens` does not change. Later stages keep
only their floor plus whatever is left.

## Checking it worked
A grant worked when the stage is declared done afterwards. A stage that asks again for the same work, or later
stages that start with nothing but their floor, mean too much was granted too early.
