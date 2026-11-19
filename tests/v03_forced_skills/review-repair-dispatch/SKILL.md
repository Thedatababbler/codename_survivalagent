---
name: review-repair-dispatch
description: Harness test. Asks for one review early and one repair right after it.
triggers:
  - when: stage_tokens >= 0.04 and reviews < 1
    action: suggest_review
  - when: reviews >= 1 and repairs < 1
    action: suggest_repair
params:
  default_quota_share: 0.1
  mode: fixed
---
## Mechanism
Harness test: one reviewer call is asked for early in the stage, and one repairer call as soon as the review is
back, whatever the state of the code.

## What to do
This run checks the sub-agent calls. Make the suggested call at the end of this message as your very next action,
before any other command. For a review, the task is to check what exists so far against the design documents. For
a repair, the task is the list of problems the review returned (or, if it returned none, one small improvement you
name). Use the quota suggested below.

## Reading the readout
`repairs` counts repairer calls against their limit and `subagents` all sub-agent calls.

## Checking it worked
The sub-agent returned and its result is in your context. Then carry on with the stage.
