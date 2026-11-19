"""Audit hook of sealed scoring (user decision 2026-11-19). Copied as ``sitecustomize.py`` into a read-only directory
at the head of the scoring interpreter's path, so it is installed before any scored code runs (an audit hook cannot
be removed). It only RECORDS: the kernel does the refusing (the scoring user owns nothing outside its scratch copy;
Landlock allows no read outside it). Reported on the inherited pipe ``ADAMAS_SCORING_AUDIT_FD``, one JSON line each:

    permission_change   chmod / chown / chgrp / setfacl (os.* calls, or such a program started) on a path outside
                        the scratch copy and the run's private tmp
    read_denied         an open of a path outside the readable roots that the kernel refused
    list_denied         a directory listing outside the readable roots that the kernel refused

Code that calls the kernel without Python's os module is refused all the same, but not recorded here.
"""

import json
import os
import sys


def _install():
    try:
        fd = int(os.environ.get("ADAMAS_SCORING_AUDIT_FD") or -1)
        roots = [os.path.realpath(r) for r in json.loads(os.environ.get("ADAMAS_SCORING_AUDIT_ROOTS") or "[]")]
    except ValueError:
        return
    if fd < 0 or "ADAMAS_SANDBOX_ORIG_PYTHONPATH" in os.environ:   # the launcher itself (its own canary check): not scored code
        return
    try:
        own = [os.path.realpath(r) for r in json.loads(os.environ.get("ADAMAS_SCORING_AUDIT_WRITE") or "[]")]
    except ValueError:
        own = []
    own = own or [os.path.realpath(os.getcwd())]
    busy = [False]
    seen = set()
    progs = ("chmod", "chown", "chgrp", "setfacl")
    _open, _close, _listdir, _write, _real, _dumps = os.open, os.close, os.listdir, os.write, os.path.realpath, json.dumps

    def inside(path, tops):
        return any(path == t or path.startswith(t.rstrip("/") + "/") for t in tops)

    def emit(event, target, how):
        key = (event, target, how)
        if key in seen or len(seen) > 2000:
            return
        seen.add(key)
        try:
            _write(fd, (_dumps({"event": event, "target": target, "how": how}) + "\n").encode("utf-8", "replace")[:3000])
        except OSError:
            pass

    def as_path(p):
        if isinstance(p, bytes):
            p = p.decode("utf-8", "replace")
        if not isinstance(p, str):
            p = os.fspath(p) if hasattr(p, "__fspath__") else None
            if isinstance(p, bytes):
                p = p.decode("utf-8", "replace")
        return _real(p) if isinstance(p, str) and p else None

    def words_of(args):
        if isinstance(args, (str, bytes)):
            args = args.decode("utf-8", "replace") if isinstance(args, bytes) else args
            return args.replace(";", " ; ").replace("&&", " ; ").replace("|", " ; ").split()
        try:
            return [a.decode("utf-8", "replace") if isinstance(a, bytes) else str(a) for a in args]
        except TypeError:
            return []

    def perm_program(words, how):
        for i, w in enumerate(words):
            if os.path.basename(w) in progs:
                for a in words[i + 1:]:
                    if a == ";":
                        break
                    if a.startswith("/") or a.startswith(".."):
                        t = _real(a)
                        if not inside(t, own):
                            emit("permission_change", t, how + ":" + os.path.basename(w))

    def hook(event, args):
        if busy[0]:
            return
        busy[0] = True
        try:
            if event in ("os.chmod", "os.chown", "os.setxattr", "os.removexattr"):
                t = as_path(args[0]) if args and not isinstance(args[0], int) else None
                if t and not inside(t, own):
                    emit("permission_change", t, event)
            elif event == "open":
                t = as_path(args[0]) if args and not isinstance(args[0], int) else None
                if t and not inside(t, roots):
                    try:
                        _close(_open(t, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)))
                    except PermissionError:
                        emit("read_denied", t, event)
                    except OSError:
                        pass
            elif event in ("os.listdir", "os.scandir"):
                t = as_path(args[0]) if args and args[0] is not None and not isinstance(args[0], int) else None
                if t and not inside(t, roots):
                    try:
                        _listdir(t)
                    except PermissionError:
                        emit("list_denied", t, event)
                    except OSError:
                        pass
            elif event == "subprocess.Popen":
                perm_program(words_of(args[1]) if len(args) > 1 else [], event)
            elif event == "os.system":
                perm_program(words_of(args[0]) if args else [], event)
            elif event in ("os.exec", "os.posix_spawn"):
                perm_program(words_of(args[1]) if len(args) > 1 else [], event)
        except Exception:  # noqa: BLE001 - a hook must never break the scored run
            pass
        finally:
            busy[0] = False

    sys.addaudithook(hook)


_install()
