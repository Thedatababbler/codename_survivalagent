---
name: subagent-handoff
description: Harness test. Asks for one worker handoff after the first repair.
triggers:
  - when: repairs >= 1 and workers < 1
    action: suggest_handoff
params:
  quota_share: 0.1
  mode: fixed
---
## Mechanism
Harness test: one worker handoff is asked for as soon as the first repair is back.

## What to do
This run checks the handoff. First rewrite the memory file so that every heading is filled in and it reflects the
code as it is now. Then, as your next action, make the suggested call at the end of this message, with a task that
names one small, well-bounded piece of this stage that is still missing. A handoff is refused while the memory
file is older than the last code change: if that happens, update it and call again.

## Reading the readout
`subagents` counts all sub-agent calls of the round against their limit.

## Checking it worked
After the handoff your context is the memory file, and it names what the worker did. Then carry on with the stage.
