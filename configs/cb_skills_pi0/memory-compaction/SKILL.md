---
name: memory-compaction
description: Baseline. The framework summarises the older conversation whenever the context is three quarters full.
triggers:
  - when: "context >= 0.75"
    action: framework_compaction
    mode: while
params:
  keep_recent_share: 0.25
  use_memory: false
---
## Mechanism
The executor's context has a fixed window and the input it is given is larger than the window. Before every call for as long as the
context is at or above the trigger, the framework replaces the older turns with a summary written by a separate model call and
keeps as they are the most recent turns that together take at most `keep_recent_share` of the window (at least the newest one). Each compaction takes one from the run's compaction pool; the summary
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

## What to keep when compacting
You are compressing the earlier part of an agent's session so that the session can continue in a smaller context.
The agent's task is given first, then the part of the conversation that will be replaced by your summary. Write the
summary for the agent itself: what has been done so far, what the agent stored outside its context and where (file
paths and formats), what the task requires the agent to still have in its context, and what is left to do. State
facts only; no advice, no speculation. Be as short as the content allows.
