# Copied from AdaMAS src/orchestra/sandbox/exec.py (34276d69) for AdaMAS-unit, 2026-11-05.
# Changed 2026-11-19 (user decision, after smoke forced-1): a file-confined command no longer runs as root. After
# the Landlock rules are in force the launcher drops to the ordinary user named in ADAMAS_SANDBOX_UID (set by
# policy.launcher_env from configs/sandbox.yaml) and refuses to run when it cannot. Landlock does not mediate
# chmod/chown; as root an agent could change the permission bits of any path.
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


def _drop_root(policy: Policy) -> str:
    """Become the ordinary agent user for good; '' when done (or when already not root), else why not. Sealed
    held-out scoring is file-confined too since 2026-11-19 and becomes the scoring user the same way."""
    if not policy.restrict_fs or os.geteuid() != 0:
        os.environ.pop("ADAMAS_SANDBOX_UID", None)
        return ""
    try:
        uid = int(os.environ.pop("ADAMAS_SANDBOX_UID", "0"))
    except ValueError:
        uid = 0
    if uid < 1000:
        return "no agent user given (ADAMAS_SANDBOX_UID)"
    try:
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    except OSError as exc:
        return f"cannot switch to user {uid}: {type(exc).__name__}"
    if os.getuid() != uid or os.geteuid() != uid or os.getgid() != uid or os.getgroups():
        return "the switch of user did not take effect"
    try:
        os.setuid(0)
    except OSError:
        pass
    else:
        return "root can be regained after the switch"
    os.environ["USER"] = os.environ["LOGNAME"] = "agent"
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
    why = _drop_root(policy)
    if why:
        print(f"adamas-sandbox: refusing to run as root: {why}", file=sys.stderr)
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
