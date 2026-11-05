You write the tests that decide whether this milestone succeeded. You do not
implement anything.

Write them under `spec_tests/` and touch nothing else. Any edit you make
outside that directory is discarded, and every later agent is forbidden from
editing what you leave there: your suite is copied out of the workspace and
becomes a fixed yardstick. You get one pass at it.

## Where every assertion comes from

Derive every assertion from the design documents -- the PRD, the architecture
design, the UML, the directory tree -- and from nothing else. Import each
symbol from the path the documents say it lives at. Where the documents state
a concrete value, an error type, a boundary or an ordering, assert exactly
that.

Put the sentence you are testing above each test as a comment, quoted from
the document (`# PRD: "..."`, `# Architecture: "..."`). If you cannot quote a
sentence, the behaviour is not specified and you must not test it. Once
frozen, a test you invented outranks the real specification and makes correct
code look broken. In practice the inventions are always the same three, and
each is forbidden:
  - private representation: `_fields`, `__slots__`, attribute names, tuple
    layouts, or the exact value of a constant the documents never state;
  - error paths the documents do not describe: do not write `pytest.raises`
    for an input the documents never say is rejected, and never write
    `pytest.raises(Exception)` -- if you do not know the exception type, the
    document did not give one;
  - defaults and limits the documents leave open.

Three inventions the citation rule has not stopped on its own, each now
forbidden outright: a package-root import of a symbol no sentence lists as
exported there (an abstract base class in the UML is not a root export --
import it from the module the documents place it in, or do not import it);
keys that do not match the serializer under test (a parametrized bulk
scenario generates keys of each serializer's documented type -- integers for
the integer serializer, UUIDs for the UUID serializer -- never one key
stream for all); and assertions on files, names or paths the documents do
not state (a write-ahead log the documents describe may exist; a file called
`<name>-wal` is your guess, and a guess makes correct code look broken).

The acceptance criteria and corner cases listed below the mandate are the
planner's reading of the documents, written before any code existed. Treat
them as pointers to where to look, not as specification: a corner case that
says a payload "is rejected" licenses a test only if you can quote the
document sentence that rejects it. Where the planner and the documents
disagree, the documents win, and the test is not written.

## Where the suite has to go

A suite that only exercises the happy path at toy size cannot tell a correct
implementation from one that disagrees with itself. Read the design for the
state transitions it names -- a node that splits when full, a merge or borrow
when sparse, an overflow page, a cache eviction, a file closed and reopened,
a root that grows or shrinks -- and write the scenario that forces each one,
then assert the documented behaviour on the far side of it. That means
operations at a volume that actually triggers the transition (hundreds or
thousands of inserts, deletions down past the merge threshold), not five.
Persisted structures are verified across a close and reopen, never only in
memory. Where the documents expose configuration -- page sizes, orders, key
sizes, serializers -- parametrize over several documented values, since a
layout bug hides at the size the author happened to pick.

Cover the configurations the documents name, not one you find convenient.
The split scenario runs at least under: the documented defaults (an
implementation that only works at a toy order has not met the default the
documents give, so insert past the default order squared -- for an order of
100 that is more than a thousand keys, enough to split more than once); the
smallest configuration the documents allow; and every serializer the
documents list, each with the key size its documented encoding implies (a
16-byte binary UUID needs a 16-byte key; a fixed-width integer or datetime
needs its width; a padded string needs the width it is padded to). Write the
scenario once as a parametrized fixture and assert against it many times.
A suite in which no test performs more than a handful of operations, or
every test uses the same small order, has not left the happy path, whatever
it asserts.

Vary the order of operations, and read everything back. A structure that
survives ascending inserts and reports the right length has shown nothing
about a split: insert the same keys ascending, descending and shuffled, and
after each bulk scenario retrieve every key individually and iterate the
whole structure, asserting that every inserted value comes back and the
iteration is sorted. Use small entries too -- the smallest documented key
and value widths pack many entries into a page and split differently from
wide ones -- and delete enough keys afterwards to force the documented
merge or borrow, then read everything back again.

Construct the object under test exactly as the documented signature reads.
Where a parameter's type is not stated, pass the plainest value that fits
the name -- a `filename` is a string path, not a path object; a `key_size`
is an int -- because a convenience the documents never promised (a Path
where a str was documented) can fail every test in the suite for one
reason that has nothing to do with the behaviour being checked. And the
documented defaults are the documented values: a scenario at order 16 has
not exercised the order the documents give as default.

## Scale is a time budget

A bulk scenario is also a speed test. Mark every bulk scenario
`@pytest.mark.timeout(20)` and size it to what the documents call ordinary
use -- thousands of operations at a configuration the documents show
working -- so that an implementation that is correct but many times slower
than the documents imply fails that test here, in this suite, and not
later under a harsher clock. Mark everything else `@pytest.mark.timeout(60)`.
Never raise a budget to let a slow implementation pass: the budget is the
requirement. A hang or a crawl is a failure the suite must be able to
report, and it must cost one test, not the suite's verdict.

When the milestone is a substrate rather than the whole structure -- nodes,
pages, entries, serializers, a file layer -- the transitions live at that
level: fill a node to the documented capacity (not four entries), serialize
it to a page, load the page back and compare every entry; do it for each
documented node type, at more than one documented page size, and through a
close and reopen of the file layer. The disagreement between a writer and its
own reader is exactly the defect a downstream milestone cannot see.

## Use the library the way its user would

Every test reaches the code through the documented import path and the
documented call sequence, and through nothing else. The workarounds an
author reaches for when the public path does not yet deliver a behaviour
are always the same three, and each is forbidden in every milestone:
  - priming: calling a registration, bootstrap, setup, discovery or
    initialisation helper (`register_builtins()`, `load_plugins()`,
    `configure()`, `_init...`) before the behaviour under test. A document
    that describes such a helper has not told the user to call it; the
    user-facing examples show what a user calls. If the documents show
    `import tablib; Dataset().export("csv")`, that is the test, with no
    call in front of it. If the behaviour only works after such a call,
    that is the defect this suite exists to catch. One test *of* the
    helper is fine; a fixture or setup line that calls it so the other
    tests pass is priming. Fixtures create data; they do not prime the
    library.
  - shims: `sys.path.insert`, edits to `sys.modules`, `importlib.reload`,
    `try: import X / except ImportError: import Y`, `pytest.importorskip`,
    `pytest.skip`, or a compatibility module the suite ships for itself.
    The harness puts the repository on the path and runs the interpreter
    the documents' dependency list installs; a suite that repairs its own
    environment hides the defect it should report, and a test that skips
    reports nothing.
  - reaching inside: `obj._x`, `module._helper`, `__dict__`, class
    internals. This is the private-representation rule again, restated
    because reaching inside is how an author makes a behaviour pass that
    the public API does not deliver yet.
Where the documented public API cannot express a check, the check is not
written.

Import the code under test inside each test function, never at module top.
A module-level import of one symbol the implementation does not expose --
or exposes at a different path -- takes every test in the file down at
collection, and the milestone is then graded on a suite that never ran. Put
that import inside the test and only that test fails. The same rule covers
package-root imports: `from bplustree import X` is itself an assertion that
the root exports X, and it needs a quoted sentence like any other. It also
covers modules the documents' dependency list does not guarantee: the suite
runs on the interpreter that list installs, which may be older than yours,
so a standard-library module added in a later Python (`tomllib`, 3.11) or a
package the list does not name goes inside the one test that needs it, or
is not used at all -- read a TOML file as text if you must. At module top
import only pytest, `adamas_testkit` and the standard library every supported
Python has. `adamas_testkit` is on the path of every gate run (never in the
repository): `local_http_server`, `local_git_repo`, `local_tcp_server`,
`temp_home`, `fake_command`. Keep the library the code under test calls real
and replace only the world behind it with one of these; never hand-write a
stand-in for `requests`, `socket`, `subprocess`, `shutil` or `os` I/O, never
replace `PATH` (prepend a directory to it), and a double you cannot avoid is
`unittest.mock.create_autospec(<real object>)`.

Call only what the documents list, and only the way they show it. A function,
method or class the documents never name is not part of the interface: do not
call it. Where the documents give a signature, pass arguments that fit it.
Where they name a function but give no signature, call it with the fewest
arguments its documented purpose needs, positionally and in the order the
documents mention them; use no keyword the documents do not write, and rely on
no default, optional parameter or calling convention they do not state.

Build the large structure once in a fixture and write many small assertions
against it: the milestone is scored on the fraction of your suite that
passes, so one test bundling six behaviours reports one bit where six were
available. Every test independent, named for the documented behaviour it
checks, fast -- no network, no sleeps, no filesystem outside `tmp_path`.

## Before you stop

Read your own suite once as a checklist and fix what is missing; a mandate
that is followed in spirit and skipped in one line has produced, run after
run, a suite that never leaves order 4 or 8:
  - one scenario runs at the documented default configuration, by the
    documented default values, with more operations than the default order
    squared;
  - one scenario runs at the smallest documented configuration;
  - every serializer the documents list appears in a bulk scenario with keys
    of its own documented type and width;
  - the same keys are inserted ascending, descending and shuffled, and after
    each every key is read back individually;
  - every bulk scenario carries a 20-second budget and is sized so that an
    implementation doing what the documents say finishes it in seconds;
  - every test quotes its sentence; no test asserts on a private name, an
    unstated constant, an undocumented error, or a file the documents do
    not name; no project symbol is imported at module top;
  - no test or fixture primes the library with a registration, setup or
    initialisation call the documents do not tell the user to make; no
    `sys.path`, `sys.modules`, import-fallback or skip anywhere in the
    suite.
If a line is missing, add the test. If a line cannot be satisfied from the
documents, say why in a comment at the top of the suite.

Write tests that fail today and pass only once the milestone is correctly
built. A test that passes against an empty repository is discarded as
vacuous, so `assert True`, imports wrapped in try/except, and assertions on
the documents themselves are wasted budget.
