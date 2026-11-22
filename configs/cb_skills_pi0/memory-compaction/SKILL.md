---
name: memory-compaction
description: Baseline. The framework summarises the older conversation when the context is three quarters full.
triggers:
  - when: "context >= 0.75"
    action: framework_compaction
params:
  keep_recent_turns: 6
  use_memory: false
---
## Mechanism
The executor's context has a fixed window and the input it is given is larger than the window. When the context
reaches the trigger, the framework replaces the older turns with a summary written by a separate model call and
keeps the most recent turns as they are. Each compaction takes one from the run's compaction pool; the summary
call's tokens are charged to the run. The memory file is not required first.

## What to do
Your earlier conversation was replaced by a summary. Whatever was only in the replaced turns is no longer in your
context; files you wrote are still on disk. Continue with the current operation.

## Reading the readout
`context` is the share of the window in use. `compressions` counts the compactions used out of the pool. A turn
that would overflow the window is undone and must be retried after compacting.

## Checking it worked
After a compaction `context` drops well below the trigger. Signs of a bad compaction: something a later operation
needs is neither in the summary, nor in the recent turns, nor on disk.
