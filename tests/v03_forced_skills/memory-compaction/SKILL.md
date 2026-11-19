---
name: memory-compaction
description: Harness test. The first overflow is left to happen; afterwards the framework compacts early.
triggers:
  - when: context >= 0.8 and compressions >= 1
    action: framework_compaction
params:
  keep_recent_turns: 2
  use_memory: false
---
## Mechanism
Harness test: nothing is compacted until the context has overflowed once, so that the undoing of the newest turns
is exercised; after the first compaction the framework compacts whenever the context is four fifths full.

## What to do
Your older turns were replaced by a summary. Check the memory file is up to date and carry on.

## Reading the readout
`context` is the share of your context window in use; `compressions` counts compactions against their limit. The
window in this run is small: print little.

## Checking it worked
After a compaction `context` is clearly lower and you still know what is done and what is left.
