You are repairing one unit of a Python repository. Its acceptance gate failed. The failing tests and their output are
in `../repair_evidence/` (read only): `failures.md` lists them by kind, and `suites/` holds the frozen test suites
the gate runs. You may run them:
`PYTHONPATH=../repair_evidence/harness python -m pytest ../repair_evidence/suites/<unit>/ -q`.

The suites are fixed and are the yardstick: change the code, never try to change, copy or work around the tests.
Read the failures, find the cause in the code, fix it, and check that the tests you fixed pass and that the other
tests in the suites still do. The design documents are in `docs/`; where a test and the documents seem to disagree,
follow the documents and say so in your last message.
