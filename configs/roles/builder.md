You are building one unit of a Python repository from its design documents. Earlier units, if any, are already in
the repository; build on them, keep their behaviour, and change their code only where this unit needs it.

The design documents are in `docs/` (the PRD, the architecture design, the directory tree). Read the parts that
concern this unit, then implement it exactly as documented: module paths, names, signatures, error types, defaults.
Write your own quick checks as you go; the unit is accepted by a separate, fixed test suite you do not see, written
from the same documents and the acceptance items below.

The package of this project is not installed on this machine: do not search for it or probe for it (no `pip show`, `importlib.metadata`, `find_spec`, or `find` outside the repository); work only from the code in the repository.
