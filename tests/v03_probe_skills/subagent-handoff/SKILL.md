---
name: subagent-handoff
description: Baseline. No handoff is suggested; the executor may still call a worker on its own.
triggers: []
params:
  quota_share: 0.2
  mode: fixed
---
## Mechanism
A worker is a sub-agent that takes over part of a stage's implementation in a fresh context. The executor pauses
while it runs, the worker's quota is taken out of the stage first, and what it does not use comes back. A handoff
is only accepted when the memory file was updated after the last code change. This baseline never suggests one.

## What to do
If you hand work to a worker: update the memory file first, describe exactly which part of the stage the worker
should implement and what is already done, and give it a quota. After it returns, your context is replaced by the
memory file, so anything you need must be written there.

## Reading the readout
`subagents` counts all sub-agent calls of the round against their limit. A worker's quota is part of
`stage tokens` from the moment it starts.

## Checking it worked
After a handoff the memory file names what the worker finished and what is left, and the package still imports. A
worker that ends with nothing written back, or with most of its quota unused, was given the wrong piece of work.
