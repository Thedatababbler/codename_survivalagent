---
name: token-reminder
description: Baseline. One reminder when the context is nearly full.
triggers:
  - when: "context >= 0.90"
    action: remind
params:
  advise: either
  repeat: false
---
## Mechanism
The executor's context has a fixed window. A turn that would overflow it is undone. This skill sends the executor
one reminder shortly before that point. It changes nothing in the context by itself.

## What to do
Your context is nearly full. A turn that overflows the window is undone and its tokens are spent anyway. Make room
before taking in more: put what you must not lose where it survives, then run `compact`.

## Reading the readout
`context` is the share of the window in use. `round tokens` is the share of the run's token allowance already
used; the run ends when it is used up.

## Checking it worked
After the reminder `context` should fall before the window overflows. A turn undone right after the reminder means
the reminder came too late or was ignored.
