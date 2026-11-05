"""pytest plugin of the gate (``-p _unit_gate_plugin``): one outcome per test node id, written as JSON.

Copied into the gate's own copy of the code next to the suites; it writes ``$UNIT_GATE_RESULTS`` (inside that copy)
after every test, so a run killed on timeout still leaves what it reached. Outcomes: passed, failed, error (setup or
teardown), skipped. A collection error of a file is recorded as ``<file>::<collection>`` = error.
"""

from __future__ import annotations

import json
import os

_RES: dict[str, str] = {}


def _dump() -> None:
    path = os.environ.get("UNIT_GATE_RESULTS")
    if path:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_RES, fh)
        os.replace(tmp, path)


def pytest_runtest_logreport(report):
    nid = report.nodeid
    if report.when == "call":
        _RES[nid] = "passed" if report.passed else "skipped" if report.skipped else "failed"
    elif report.failed:
        _RES[nid] = "error"
    elif report.skipped and nid not in _RES:
        _RES[nid] = "skipped"
    if report.when == "teardown":
        _dump()


def pytest_collectreport(report):
    if report.failed:
        _RES[f"{report.nodeid}::<collection>"] = "error"
        _dump()


def pytest_sessionfinish(session, exitstatus):
    _dump()
