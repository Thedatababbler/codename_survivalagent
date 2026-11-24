## Rules for a rewrite in this setting

- A skill must not tell the executor to do what its task instructions forbid. Some tasks tell the executor not to
  write to disk (for example one that asks it to keep marked lines in its context, or one that asks it to keep a
  board in its context); others allow the disk as scratch space. You are not told which task a run is. So a
  skill must never tell the executor, without condition, to write the content it is given to a file, to the memory
  file, or to any other place outside its context; anything it says about the disk must leave the task's own
  instructions in force ("where your task allows it"), and it must never say that those instructions can be ignored.
- Whether a run wrote task content to disk is measured. A combination whose runs break the instructions of the
  disk-forbidding tasks more often than the baseline's runs do is discarded, whatever its scores.
- On the disk-forbidding tasks the executor has no memory file and is not told of one (the others have
  `AGENT_MEMORY.md`). A skill must therefore not rely on a memory file being there, and must not name it without
  the condition above.
- The task's instructions come before the management text: the executor is told so.
