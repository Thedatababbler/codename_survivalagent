# Copied from AdaMAS src/orchestra/sandbox/exec.py (34276d69) for AdaMAS-unit, 2026-11-05.
"""Confine, then exec: ``python -m orchestra.sandbox.exec --policy <json> -- <command...>``.

Kept free of orchestra imports beyond ``landlock`` so the launcher itself reads
nothing it will not be allowed to read afterwards.
"""

from __future__ import annotations

import json
import os
import sys

from orchestra.sandbox.landlock import Policy, abi_version, apply

# a file no confined process may read: if it opens after apply(), the rules are not in force
_CANARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "configs", "sandbox.yaml")


def _verify(policy: Policy) -> str:
    """'' when the rules just applied are really in force, else why not (sandbox-only rule, 2026-10-11)."""
    if policy.restrict_net and abi_version() < 4:
        return f"landlock ABI {abi_version()} cannot restrict TCP"
    if policy.restrict_fs:
        try:
            fd = os.open(os.path.normpath(_CANARY), os.O_RDONLY)
        except PermissionError:
            pass
        except OSError as exc:
            return f"canary check inconclusive: {type(exc).__name__}"
        else:
            os.close(fd)
            return "a forbidden file is still readable after landlock_restrict_self"
    if policy.restrict_net and not any(os.path.basename(p).startswith("libnetguard") for p in
                                       (os.environ.get("LD_PRELOAD") or "").split(":") if p):
        return "LD_PRELOAD does not carry the network guard"
    return ""


def main(argv: list[str]) -> int:
    if "--" not in argv or argv[:1] != ["--policy"]:
        print("usage: python -m orchestra.sandbox.exec --policy <file> -- <command...>", file=sys.stderr)
        return 2
    policy_path = argv[1]
    cmd = argv[argv.index("--") + 1:]
    with open(policy_path, encoding="utf-8") as fh:
        policy = Policy.from_dict(json.load(fh))
    apply(policy)
    why = _verify(policy)
    if why:
        print(f"adamas-sandbox: refusing to run unconfined: {why}", file=sys.stderr)
        return 126
    # the launcher's own import path is not the confined command's
    orig = os.environ.pop("ADAMAS_SANDBOX_ORIG_PYTHONPATH", None)
    if orig is not None:
        if orig:
            os.environ["PYTHONPATH"] = orig
        else:
            os.environ.pop("PYTHONPATH", None)
    os.execvp(cmd[0], cmd)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
