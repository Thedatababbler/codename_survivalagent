"""(Copied from AdaMAS 34276d69 src/orchestra/control/author/fixture_check.py.) Fixture-preparation check before a gate suite is frozen (user decision 2026-10-11, after A4's simplejwt suite).

Runs only the setup phase of every test (``pytest --setup-only``) in the task environment, inside the sandbox,
on the pre-implementation workspace. Violations go back to the author through the review's fix rounds:

- ``fixture_setup``: a fixture's setup raised. An ``ImportError`` of the project package itself is expected before
  the implementation exists and is not a violation (the same tolerance custody applies to collection).
- ``fixture_none``: a fixture from outside the suite (a plugin, pytest) yielded ``None``
  (``adamas_testkit.fixture_probe``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from orchestra.codeprojecteval.suite_audit import Violation

_ERR = re.compile(r"^ERROR (\S+?)(?:\s+-\s+(.*))?$", re.M)
_NONE = re.compile(r"^ADAMAS_FIXTURE_NONE (\S+) (\S+)$", re.M)
_IMPORT = re.compile(r"\b(?:ModuleNotFoundError|ImportError)\b.*?['\"]([\w.]+)['\"]")


def _project_import_error(message: str, packages: Iterable[str]) -> bool:
    m = _IMPORT.search(message or "")
    return bool(m) and m.group(1).split(".")[0] in set(packages)


_SECTION = re.compile(r"^_{3,} ERROR at setup of (\S+) _{3,}$(.*?)(?=^_{3,} |^={3,} |\Z)", re.M | re.S)


def _messages(output: str) -> dict[str, str]:
    """test name -> the first ``E   `` line of its setup-error section."""
    out = {}
    for m in _SECTION.finditer(output):
        e = next((ln[1:].strip() for ln in m.group(2).splitlines() if ln.startswith("E ")), "")
        out[m.group(1).split("[")[0]] = e
    return out


def parse(output: str, packages: Iterable[str]) -> list[Violation]:
    out: list[Violation] = []
    pk = list(packages)
    msgs = _messages(output)
    for m in _ERR.finditer(output):
        node = m.group(1)
        # the error section's line is whole; the short summary truncates it ("No module nam...")
        msg = msgs.get(node.split("::")[-1].split("[")[0], "") or (m.group(2) or "").strip()
        if "::" not in node or _project_import_error(msg, pk):
            continue
        f, case = node.split("::", 1)
        out.append(Violation("fixture_setup", Path(f).name, case.split("::")[-1], 0,
                             "fixture setup fails in the task environment before any implementation: "
                             + (msg[:200] or "an error in a fixture's setup (run pytest --setup-only on the suite to see it)")))
    seen = set()
    for m in _NONE.finditer(output):
        fixture, node = m.group(1), m.group(2)
        f, _, case = node.partition("::")
        key = (fixture, case)
        if key in seen:
            continue
        seen.add(key)
        out.append(Violation("fixture_none", Path(f).name, case.split("::")[-1], 0,
                             f"fixture {fixture!r} (from a plugin, not the suite) is None in the task environment: "
                             "it is not configured there; do not use it"))
    return out


def check(workspace: str | Path, *, python: str | Path, packages: Iterable[str], testkit: str | Path | None,
          suite_dir: str = "spec_tests", timeout: int = 300) -> tuple[list[Violation], str]:
    """(violations, raw output tail) for the suite at ``workspace/suite_dir``; runs confined."""
    from orchestra.sandbox.guard import run_confined

    ws = Path(workspace)
    if not (ws / suite_dir).is_dir():
        return [], ""
    paths = [str(testkit)] if testkit else []
    paths += [str(ws)] + ([str(ws / "src")] if (ws / "src").is_dir() else [])
    env = {"PYTHONPATH": os.pathsep.join(paths), "PATH": f"{Path(python).parent}:/usr/bin:/bin", "HOME": str(ws),
           "PYTHONDONTWRITEBYTECODE": "1"}
    # AdaMAS-unit 2026-11-07: the configuration file pinned inside the workspace (pytest >= 8 otherwise takes an
    # ancestor's and collects from there, which the sandbox refuses)
    ini = ws / "_unit_pytest.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    cmd = [str(python), "-m", "pytest", "-c", str(ini), "--setup-only", "-q", "-s", "-rE", "--tb=short", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
           "--continue-on-collection-errors", f"--rootdir={ws / suite_dir}", f"--confcutdir={ws / suite_dir}"]
    if testkit:
        cmd += ["-p", "adamas_testkit.fixture_probe"]
    cmd += [str(ws / suite_dir)]
    try:
        proc = run_confined(cmd, cwd=ws, env=env, role="author_fixture_check", capture_output=True, text=True,
                            timeout=timeout, check=False)
        output = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 -- the check never blocks the author on its own failure
        return [], f"fixture check did not run: {type(exc).__name__}: {exc}"
    return parse(output, packages), output[-3000:]


__all__ = ["check", "parse"]
