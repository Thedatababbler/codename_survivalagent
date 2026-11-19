---
name: completion-check
description: Baseline. Every declaration that a stage is done is accepted as it comes.
triggers:
  - when: "true"
    action: accept
params:
  idle_prompts_to_accept: 2
  max_prompts: 3
  max_refusals: 2
---
## Mechanism
When the executor declares a stage done (declaring the last stage submits the repository), this skill is asked
first. It may accept the declaration, hold it and prompt the executor to keep checking and improving, or hold it
until a review was made or the executor's own tests pass. A held declaration leaves the stage open and its tokens
keep being spent. This baseline accepts every declaration.

## What to do
Go over this stage's work once more against the design documents, complete what is missing or wrong, and declare
the stage done again.

## Reading the readout
A declaration may be held; the answer then says what is asked before it is accepted. The stage stays open
meanwhile, so `stage tokens` keeps growing. In this baseline a declaration is not held.

## Checking it worked
A held declaration worked when the work done after it changed the code for the better and the stage was then
declared done. Prompts that change nothing, or a stage that runs out while held, mean the check cost more than it
gave.
