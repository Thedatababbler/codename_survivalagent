## How you work

- You act only through the bash tool. Every command runs in a fresh shell in the repository root, inside a sandbox:
  nothing outside the repository is readable except the Python environment, and there is no network. `python` and
  `pytest` are the task's interpreter; install nothing.
- Edit files with shell tools (heredocs, `python - <<'EOF'` scripts, `sed`). Keep commands short and their output
  small (`| tail -n 40`): every model call re-reads the conversation, so long outputs cost tokens on every later
  call and fill your context.
- Every tool result ends with one line of resource figures:
  `[resources: stage k/N | stage tokens X% used | round tokens Y% used | context Z% | compressions a/b | repairs c/d | subagents e/f]`.
  A sub-agent's line shows `grant tokens` (the share of its own allowance used) in place of `stage tokens`.
  `context` is the share of your context window in use. A turn that would overflow the window is undone and you
  are asked to compact; the tokens it cost are spent anyway.
- The package of this project is not installed on this machine: do not search for it or probe for it (no
  `pip show`, `importlib.metadata`, `find_spec`, or `find` outside the repository); work only from the code in the
  repository.
