# Copied from AdaMAS src/orchestra/sandbox/landlock.py (34276d69) for AdaMAS-unit, 2026-11-05.
"""Landlock confinement for agent processes (sandbox spec A, 2026-10-09).

The host is a Docker container without CAP_SYS_ADMIN / CAP_NET_ADMIN and with
user namespaces refused, so mount, network namespaces, bubblewrap and firewall
rules are all unavailable. Landlock needs no privilege, and the kernel here
provides ABI 4, which covers both:
- filesystem: every access outside the allowed trees fails with EACCES at
  open time, including ``readdir``, so names are not listed either;
- TCP connect by port: only the allowed ports can be reached.

A confined process cannot lift the restriction, and every child inherits it.

``apply(policy)`` confines the calling process; ``orchestra.sandbox.exec``
applies it and then ``execve``s the agent command.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_SYS_CREATE, _SYS_ADD, _SYS_RESTRICT = 444, 445, 446      # x86_64
_RULE_PATH_BENEATH, _RULE_NET_PORT = 1, 2
_PR_SET_NO_NEW_PRIVS = 38

FS = {
    "execute": 1 << 0, "write_file": 1 << 1, "read_file": 1 << 2, "read_dir": 1 << 3,
    "remove_dir": 1 << 4, "remove_file": 1 << 5, "make_char": 1 << 6, "make_dir": 1 << 7,
    "make_reg": 1 << 8, "make_sock": 1 << 9, "make_fifo": 1 << 10, "make_block": 1 << 11,
    "make_sym": 1 << 12, "refer": 1 << 13, "truncate": 1 << 14,
}
NET_BIND, NET_CONNECT = 1 << 0, 1 << 1
FILE_ONLY = FS["execute"] | FS["write_file"] | FS["read_file"] | FS["truncate"]
READ = FS["execute"] | FS["read_file"] | FS["read_dir"]
WRITE = READ | sum(v for k, v in FS.items() if k not in ("execute", "read_file", "read_dir"))


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _NetPort(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


class LandlockError(OSError):
    pass


_libc = ctypes.CDLL(None, use_errno=True)


def abi_version() -> int:
    return int(_libc.syscall(_SYS_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1)))


@dataclass
class Policy:
    """What a confined process may touch. Paths that do not exist are skipped (and reported)."""

    read: list[str] = field(default_factory=list)       # read + execute, recursively
    write: list[str] = field(default_factory=list)      # read, write, create, delete, recursively
    connect_ports: list[int] = field(default_factory=list)   # TCP ports connect() may reach
    restrict_net: bool = True
    restrict_fs: bool = True     # False: network rules only (held-out scoring keeps its file access)
    list_dirs: list[str] = field(default_factory=list)  # names may be listed (readdir), no file in them read

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Policy:
        return cls(read=list(d.get("read") or []), write=list(d.get("write") or []),
                   connect_ports=[int(p) for p in d.get("connect_ports") or []], restrict_net=bool(d.get("restrict_net", True)),
                   restrict_fs=bool(d.get("restrict_fs", True)), list_dirs=list(d.get("list_dirs") or []))

    def to_dict(self) -> dict[str, Any]:
        return {"read": self.read, "write": self.write, "connect_ports": self.connect_ports, "restrict_net": self.restrict_net,
                "restrict_fs": self.restrict_fs, "list_dirs": self.list_dirs}


def _add_path(ruleset: int, path: str, access: int) -> bool:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        if not os.path.isdir(path):
            access &= FILE_ONLY
        attr = _PathBeneath(allowed_access=access, parent_fd=fd)
        if _libc.syscall(_SYS_ADD, ruleset, _RULE_PATH_BENEATH, ctypes.byref(attr), ctypes.c_uint32(0)) != 0:
            raise LandlockError(ctypes.get_errno(), f"landlock_add_rule({path})")
        return True
    finally:
        os.close(fd)


def apply(policy: Policy) -> dict[str, Any]:
    """Confine this process (and every later child). Returns what was granted, for the manifest."""
    abi = abi_version()
    if abi < 1:
        raise LandlockError(ctypes.get_errno(), "landlock unavailable")
    handled_fs = sum(FS.values()) if abi >= 3 else sum(v for k, v in FS.items() if k != "truncate")
    if abi < 2:
        handled_fs &= ~FS["refer"]
    if not policy.restrict_fs:
        handled_fs = 0
    handled_net = (NET_CONNECT if policy.restrict_net else 0) if abi >= 4 else 0
    attr = _RulesetAttr(handled_access_fs=handled_fs, handled_access_net=handled_net)
    ruleset = _libc.syscall(_SYS_CREATE, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if ruleset < 0:
        raise LandlockError(ctypes.get_errno(), "landlock_create_ruleset")
    granted: dict[str, Any] = {"abi": abi, "read": [], "write": [], "missing": [], "connect_ports": 0}
    try:
        for p in policy.write:
            (granted["write"] if _add_path(ruleset, p, WRITE & handled_fs) else granted["missing"]).append(p)
        for p in policy.read:
            (granted["read"] if _add_path(ruleset, p, READ & handled_fs) else granted["missing"]).append(p)
        for p in policy.list_dirs:
            if os.path.isdir(p):
                _add_path(ruleset, p, FS["read_dir"] & handled_fs)
        if handled_net:
            for port in sorted(set(policy.connect_ports)):
                na = _NetPort(allowed_access=NET_CONNECT, port=port)
                if _libc.syscall(_SYS_ADD, ruleset, _RULE_NET_PORT, ctypes.byref(na), ctypes.c_uint32(0)) != 0:
                    raise LandlockError(ctypes.get_errno(), f"landlock_add_rule(port {port})")
            granted["connect_ports"] = len(set(policy.connect_ports))
        if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LandlockError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS)")
        if _libc.syscall(_SYS_RESTRICT, ruleset, ctypes.c_uint32(0)) != 0:
            raise LandlockError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(ruleset)
    granted["net_restricted"] = bool(handled_net)
    return granted


def port_ranges(ranges: Iterable[tuple[int, int]]) -> list[int]:
    return [p for lo, hi in ranges for p in range(lo, hi + 1)]


__all__ = ["FS", "LandlockError", "NET_CONNECT", "Policy", "abi_version", "apply", "port_ranges"]
