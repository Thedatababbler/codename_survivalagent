You are making names importable in a Python repository, nothing else. A mechanical check, run from the design
documents alone, found names the documents give that cannot be imported, modules that fail to import, or `__all__`
entries that do not exist. The list and the documents' lines about each name are in the task below; the full design
documents are in `docs/`.

What you may do:
- correct an import path, or import a name where the documents say it can be imported from;
- export a name from a package's `__init__.py`, or correct `__all__`;
- add a minimal importable definition of a listed missing name: re-export an existing implementation that has another
  name or lives elsewhere, or a small placeholder (a class or function whose body raises `NotImplementedError`, a
  constant with the documented value).

What you may not do: change the body of any existing function or class, implement new behaviour, touch tests or any
non-Python file, or define names that are not listed. A program checks your change afterwards; a change outside these
limits, or longer than the line limit, is undone as a whole. You get one session with a small fixed budget and no
top-ups (`request_budget` is refused here). Check your change with
`python -c "from <module> import <name>"` and stop.
