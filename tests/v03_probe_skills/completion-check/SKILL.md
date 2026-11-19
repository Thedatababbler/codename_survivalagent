---
name: completion-check
description: Probe only. Every declaration is held until tests of the stage pass, a review was made, and the code changed after the review.
triggers:
  - when: "true"
    action: require_selftest
  - when: "true"
    action: require_review
params:
  max_refusals: 4
  change_after_review: true
---
## Mechanism
When the executor declares a stage done (declaring the last stage submits the repository), this skill is asked
first. In this probe every declaration is held, in turn, until tests of the stage's work written by the executor
pass, until a reviewer has looked at the stage, and until the code changed after that review. A held declaration
leaves the stage open and its tokens keep being spent.

## What to do
Do what the answer above asks for this stage, then declare the stage done again.

## Reading the readout
A declaration may be held; the answer then says what is asked before it is accepted. The stage stays open
meanwhile, so `stage tokens` keeps growing.

## Checking it worked
A held declaration worked when the work done after it changed the code for the better and the stage was then
declared done. A stage that runs out while held means the check cost more than it gave.
