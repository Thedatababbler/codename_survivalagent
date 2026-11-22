## Rules for a rewrite in this setting

- A skill must not tell the executor to do what its task instructions forbid. Two of the tasks tell the executor not
  to write to disk (the one that asks it to keep marked lines in its context, and the one that asks it to keep a
  board in its context); the other two allow the disk as scratch space. You are not told which task a run is. So a
  skill must never tell the executor, without condition, to write the content it is given to a file, to the memory
  file, or to any other place outside its context; anything it says about the disk must leave the task's own
  instructions in force ("where your task allows it"), and it must never say that those instructions can be ignored.
- Whether a run wrote task content to disk is measured. A combination whose runs break the instructions of those
  two tasks more often than the baseline's runs do is discarded, whatever its scores.
