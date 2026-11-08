"""Sealed readers of the held-out suite (copied pattern from AdaMAS 34276d69, scripts/sealed/).

Only modules in this directory may open the held-out directory (``_guard.heldout_dir`` checks the call stack). Every
script here prints numbers only; ``_guard.assert_no_source_overlap`` checks the output before it leaves.
"""
