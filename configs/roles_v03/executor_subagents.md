You may pay for help out of the current stage's tokens with
`subagent type=<review|repair|worker> budget=<0..1> task="<what to do>"` (a command of its own; `budget` is the
share of what this stage has left, and is required). You pause while the sub-agent runs; what it does not use
comes back.
- `review`: reads the code against the design documents and returns a list of problems; it cannot change files.
- `repair`: changes code to fix a list of problems or failing checks that you give it.
- `worker`: takes over part of this stage's implementation in a fresh context. Update the memory file first (the
  handoff is refused otherwise); when the worker returns, your context is replaced by the memory file.
Each kind has a call limit for the whole run, and there is a limit on all calls together.
