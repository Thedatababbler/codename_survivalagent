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
The executor's context has a fixed window. When the context reaches the trigger, the framework replaces the older
turns with a summary written by a separate model call and keeps the most recent turns as they are. Each compaction
takes one from the round's compaction pool; the summary call's tokens are charged to the stage. The memory file is
not required first.

## What to do
Your earlier conversation was replaced by a summary. Check the summary against the repository before relying on it
(list the files, rerun your quick checks), then continue with the stage.

## Reading the readout
`context` is the share of the window in use. `compressions` counts the compactions used out of the pool. A turn
that would overflow the window is undone and must be retried after compacting.

## Checking it worked
After a compaction `context` drops well below the trigger. Signs of a bad compaction: the same files are read
again in full, or finished work is redone.
