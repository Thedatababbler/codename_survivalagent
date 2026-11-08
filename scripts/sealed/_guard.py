"""Call-stack guard for the held-out suite and the dataset reference (spec §5).

``heldout_dir`` and ``reference_root`` are the only functions that hand out
those paths, and they raise ``SealedAccessError`` unless some frame on the
call stack belongs to a module under ``scripts/sealed/``. A thin shim
elsewhere may *run* a sealed script (``runpy``), because the sealed script's
own frames are then on the stack; it cannot import a helper and read the
directory itself.

``assert_no_source_overlap`` is the output check the spec's B2 test names: no
20-character window of what a sealed script writes may occur in any held-out
source file.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path

SEALED_DIR = Path(__file__).resolve().parent
DATASET_ROOT = Path(os.environ.get("CPE_DATASET_ROOT") or "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
ENV_ROOT = Path(os.environ.get("CPE_ENV_ROOT") or "/root/codex-benchmarks/cpe_envs")
SHINGLE = 20


class SealedAccessError(PermissionError):
    """A module outside scripts/sealed/ asked for the held-out suite or the reference."""


def _caller_is_sealed() -> bool:
    for frame in inspect.stack()[1:]:
        try:
            f = Path(frame.filename).resolve()
        except (OSError, RuntimeError):
            continue
        if f.parent == SEALED_DIR and f.name != "_guard.py":
            return True
    return False


def require_sealed_caller(what: str) -> None:
    if not _caller_is_sealed():
        raise SealedAccessError(f"{what} may only be read by a module under scripts/sealed/")


NL2_DATASET_ROOT = Path(os.environ.get("NL2_DATASET_ROOT") or "/root/codex-benchmarks/nl2repo/cpe_format")
NL2_ENV_ROOT = Path(os.environ.get("NL2_ENV_ROOT") or "/root/codex-benchmarks/nl2repo/envs")


def dataset_root_for(task: str) -> Path:
    """NL2Repo tasks (``nl2_*``) live under their own converted dataset root."""
    if task.startswith("nl2_") and (NL2_DATASET_ROOT / task).is_dir():
        return NL2_DATASET_ROOT
    return DATASET_ROOT


def env_root_for(task: str) -> Path:
    if task.startswith("nl2_") and (NL2_ENV_ROOT / task).is_dir():
        return NL2_ENV_ROOT
    return ENV_ROOT


def task_config(task: str) -> dict:
    return json.loads((dataset_root_for(task) / task / "config.json").read_text(encoding="utf-8"))


def heldout_dir(task: str) -> Path:
    """The dataset's ``unit_tests`` directory of ``task``. Sealed callers only."""
    require_sealed_caller("the held-out suite")
    cfg = task_config(task)
    return dataset_root_for(task) / task / str(cfg.get("unit_tests") or "unit_tests")


def heldout_subdir_name(task: str) -> str:
    """The relative name the held-out directory has inside a task repository ('unit_tests')."""
    return str(task_config(task).get("unit_tests") or "unit_tests")


def reference_root(task: str) -> Path:
    """The dataset's reference implementation root of ``task``. Sealed callers only."""
    require_sealed_caller("the reference implementation")
    return dataset_root_for(task) / task


def sealed_case_id(node_id: str) -> str:
    """A held-out node id fit to leave a sealed script: parametrize ids carry literals from the
    held-out source (URLs, sample values), so ``test_x[git+https://...]`` becomes
    ``test_x[#3fa9c2d1]`` -- still distinct per parameter, no literal."""
    import hashlib
    if "[" in node_id and node_id.endswith("]"):
        base, param = node_id.split("[", 1)
        return f"{base}[#{hashlib.sha1(param[:-1].encode()).hexdigest()[:8]}]"
    return node_id


def task_python(task: str) -> Path:
    return env_root_for(task) / task / "bin" / "python"


def _windows(text: str, n: int = SHINGLE) -> set[str]:
    t = " ".join(text.split())
    return {t[i : i + n] for i in range(0, max(0, len(t) - n + 1))}


def heldout_source_windows(task: str, n: int = SHINGLE) -> set[str]:
    """Every ``n``-character window of the held-out sources (normalised whitespace)."""
    require_sealed_caller("the held-out suite")
    out: set[str] = set()
    for p in heldout_dir(task).rglob("*.py"):
        out |= _windows(p.read_text(encoding="utf-8", errors="replace"), n)
    return out


_ID_RE = re.compile(r"[\w./-]+\.py::[\w\[\]:.\-]+(?:\[[^\]\"]*\])?")
#: a quoted string that is nothing but a (dotted) identifier: a public symbol name
_SYMBOL_STR_RE = re.compile(r"\"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\"")


def assert_no_source_overlap(output_text: str, task: str, *, n: int = SHINGLE, allow_case_ids: bool = True,
                             allow_symbols: bool = True) -> None:
    """Raise if any ``n``-char window of ``output_text`` occurs in the held-out sources.

    Case ids (``file.py::test_name``) and quoted bare identifiers (symbol names such as
    ``"Table.insert"``) are what a sealed script is meant to output, so they are blanked
    first; what remains (keys, numbers, punctuation, and any literal or assertion text
    that slipped in) is checked.
    """
    text = _ID_RE.sub(" ", output_text) if allow_case_ids else output_text
    if allow_symbols:
        text = _SYMBOL_STR_RE.sub(" ", text)
    windows = _windows(text, n)
    if not windows:
        return
    overlap = windows & heldout_source_windows(task, n)
    # windows made only of punctuation / whitespace / short tokens are not source
    overlap = {w for w in overlap if re.search(r"[A-Za-z]{4,}", w)}
    if overlap:
        sample = sorted(overlap)[:3]
        raise SealedAccessError(f"sealed output overlaps held-out source in {len(overlap)} window(s), e.g. {sample!r}")


__all__ = [
    "DATASET_ROOT", "ENV_ROOT", "dataset_root_for", "env_root_for", "sealed_case_id", "SEALED_DIR", "SHINGLE", "SealedAccessError", "assert_no_source_overlap",
    "heldout_dir", "heldout_source_windows", "heldout_subdir_name", "reference_root", "require_sealed_caller",
    "task_config", "task_python",
]
