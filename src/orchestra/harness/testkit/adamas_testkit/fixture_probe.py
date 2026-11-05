"""pytest plugin for the fixture-preparation check (``-p adamas_testkit.fixture_probe``, 2026-10-11).

Flags a fixture from outside the suite (a plugin or pytest itself) that the suite asks for by name (in a test's or
a suite fixture's arguments) and whose value is ``None``, e.g. pytest-django's ``django_db_blocker`` when
pytest-django is not configured. A plugin's own autouse machinery returning ``None`` is not flagged, and neither
is a suite fixture that returns ``None`` (autouse setup fixtures do). Checked from the requester's side, so a
session fixture served from cache is still seen. Prints ``ADAMAS_FIXTURE_NONE <fixture> <test nodeid>`` lines.
"""

from __future__ import annotations

import inspect
import os
import sys

import pytest

_SEEN: set[tuple[str, str]] = set()


def _in_suite(func, root: str) -> bool:
    try:
        return os.path.realpath(inspect.getfile(func)).startswith(root + os.sep)
    except (TypeError, OSError):
        return False


def _third_party(request, name: str, root: str) -> bool:
    try:
        defs = request._fixturemanager.getfixturedefs(name, request.node)
    except Exception:  # noqa: BLE001
        return False
    if not defs:
        return False
    return not _in_suite(defs[-1].func, root)


def _report(name: str, nodeid: str) -> None:
    if (name, nodeid) not in _SEEN:
        _SEEN.add((name, nodeid))
        sys.__stdout__.write(f"\nADAMAS_FIXTURE_NONE {name} {nodeid}\n")
        sys.__stdout__.flush()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Before a suite fixture runs: its named arguments are already set up; check them."""
    root = os.path.realpath(str(request.config.rootpath))
    if _in_suite(fixturedef.func, root):
        for name in fixturedef.argnames:
            if name == "request" or not _third_party(request, name, root):
                continue
            try:
                value = request.getfixturevalue(name)
            except Exception:  # noqa: BLE001
                continue
            if value is None:
                _report(name, getattr(request.node, "nodeid", "?"))
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """After a test's setup: the plugin fixtures it named itself."""
    yield
    root = os.path.realpath(str(item.config.rootpath))
    req = getattr(item, "_request", None)
    for name in getattr(getattr(item, "_fixtureinfo", None), "argnames", ()) or ():
        if name in getattr(item, "funcargs", {}) and item.funcargs[name] is None and req is not None \
                and _third_party(req, name, root):
            _report(name, item.nodeid)
