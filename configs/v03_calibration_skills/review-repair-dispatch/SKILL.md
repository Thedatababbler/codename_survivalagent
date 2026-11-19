---
name: review-repair-dispatch
description: Baseline. The executor decides alone whether to call a reviewer or a repairer; nothing is suggested.
triggers: []
params:
  default_quota_share: 0.2
  mode: fixed
---
## Mechanism
A reviewer reads the code against the design documents and returns a list of problems; it cannot change files. A
repairer changes code to fix a given list of problems or failing checks. Both are paid from the stage: the quota is
taken out before the call and the unused part comes back. This baseline never suggests a call; when the executor
calls one, the suggested quota is a fixed share of what the stage has left.

## What to do
If you call a reviewer, say which part of the stage it should check against which documents. If you call a
repairer, give it the concrete list of problems or failures. Name the quota in the call.

## Reading the readout
`repairs` counts repairer calls against their limit and `subagents` all sub-agent calls. Each call lowers what the
stage has left by its quota until the sub-agent returns.

## Checking it worked
A review worked when it returns specific, checkable problems; a repair worked when the listed problems are gone
and nothing that passed before fails now. A repair that returns with the same failures should not be repeated
unchanged.
