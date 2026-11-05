"""adamas_testkit: real local stand-ins for the outside world, for authored gate and verification suites.

The principle (author fake-object fix, 2026-10-09): the library the program under
test calls stays real; only the world it talks to is replaced by a real local
one. Every tool here is a context manager:

* ``local_http_server(files)``   -> base URL of an HTTP server on 127.0.0.1 (plain or streamed bodies)
* ``local_git_repo(files, ...)`` -> path of a real git repository (``.url`` gives ``file://``)
* ``local_tcp_server(script)``   -> (host, port) of a TCP server that follows a scripted dialogue
* ``temp_home()``                -> an isolated HOME / XDG config directory
* ``fake_command(name, script)`` -> a command on PATH, *prepended* to the original PATH,
  run by an absolute-path interpreter

This package lives in the run's harness directory. Gate and verification runs
put it on the import path; it is never copied into a workspace and the
held-out scoring does not load it.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["GitRepo", "HttpServer", "fake_command", "local_git_repo", "local_http_server", "local_tcp_server", "temp_home"]


# --------------------------------------------------------------------------- HTTP


@dataclass
class HttpServer:
    url: str
    requests: list[dict] = field(default_factory=list)     # method, path, headers of every request served

    def __str__(self) -> str:
        return self.url


def _response(spec) -> tuple[int, dict, bytes, bool]:
    """A file entry: bytes / str body, or {"status", "headers", "body", "stream"}."""
    if isinstance(spec, Mapping):
        body = spec.get("body", b"")
        body = body.encode() if isinstance(body, str) else bytes(body)
        return int(spec.get("status", 200)), dict(spec.get("headers") or {}), body, bool(spec.get("stream", False))
    body = spec.encode() if isinstance(spec, str) else bytes(spec)
    return 200, {}, body, False


@contextlib.contextmanager
def local_http_server(files: Mapping[str, object], *, stream: bool = False) -> Iterator[HttpServer]:
    """Serve ``files`` ({"/path": body | {...}}) on 127.0.0.1:<random port>; unknown paths answer 404.

    ``stream=True`` (or a file's ``"stream": True``) sends the body chunked, as a
    large download would arrive; ``requests.get(url, stream=True)`` and
    ``urllib.request.urlopen`` both read it.
    """
    table = {("/" + k.lstrip("/")): v for k, v in files.items()}
    server = HttpServer(url="")

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def _serve(self, head: bool) -> None:
            server.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})
            path = self.path.split("?", 1)[0]
            if path not in table:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status, headers, body, chunked = _response(table[path])
            chunked = chunked or stream
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if head:
                return
            if chunked:
                for i in range(0, len(body), 8192):
                    part = body[i:i + 8192]
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(body)

        def do_GET(self):
            self._serve(False)

        def do_HEAD(self):
            self._serve(True)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self._serve(False)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    server.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- git


class GitRepo(type(Path())):  # a Path with a file:// url
    @property
    def url(self) -> str:
        return self.resolve().as_uri()


def _git(cwd: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "testkit", "GIT_AUTHOR_EMAIL": "testkit@localhost", "GIT_COMMITTER_NAME": "testkit",
           "GIT_COMMITTER_EMAIL": "testkit@localhost", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def _write(root: Path, files: Mapping[str, object]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            p.write_bytes(bytes(content))
        else:
            p.write_text(str(content), encoding="utf-8")


@contextlib.contextmanager
def local_git_repo(files: Mapping[str, object], *, commits: Iterable[Mapping[str, object]] = (), branches: Mapping[str, Mapping[str, object]] | None = None,
                   tags: Iterable[str] = (), name: str = "repo", bare: bool = False) -> Iterator[GitRepo]:
    """A real git repository in a temporary directory.

    ``files`` is the first commit on ``main``; each mapping in ``commits`` is one
    more commit; ``branches`` maps a branch name to the files it adds on top of
    ``main``; ``tags`` tag the last ``main`` commit. ``bare=True`` returns a bare
    clone (what a server would hold). Clone it with ``git clone <path or .url>``.
    """
    with tempfile.TemporaryDirectory(prefix="adamas-testkit-git-") as d:
        work = Path(d) / name
        work.mkdir()
        _git(work, "init", "-q", "-b", "main")
        _write(work, files)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "--allow-empty", "-m", "initial")
        for i, extra in enumerate(commits, 1):
            _write(work, extra)
            _git(work, "add", "-A")
            _git(work, "commit", "-q", "--allow-empty", "-m", f"commit {i}")
        for tag in tags:
            _git(work, "tag", tag)
        for branch, extra in (branches or {}).items():
            _git(work, "checkout", "-q", "-b", branch, "main")
            _write(work, extra)
            _git(work, "add", "-A")
            _git(work, "commit", "-q", "--allow-empty", "-m", f"branch {branch}")
            _git(work, "checkout", "-q", "main")
        if bare:
            target = Path(d) / f"{name}.git"
            _git(Path(d), "clone", "-q", "--bare", str(work), str(target))
            yield GitRepo(target)
        else:
            yield GitRepo(work)


# --------------------------------------------------------------------------- TCP


@contextlib.contextmanager
def local_tcp_server(script: Iterable[tuple[bytes | None, bytes]] | Callable[[socket.socket], None], *,
                     greeting: bytes = b"") -> Iterator[tuple[str, int]]:
    """A TCP server on 127.0.0.1:<random port> for protocol clients.

    ``script`` is a list of (expected line prefix or None, reply): for every
    line the client sends, the next pair's reply is written back (the prefix,
    when given, must match, else the server answers ``b"BAD unexpected\\r\\n"``).
    ``greeting`` is sent on connect. A callable gets the connected socket and
    runs any dialogue itself. One connection at a time; the server lives for
    the ``with`` block.
    """
    steps = list(script) if not callable(script) else []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if callable(script):
                script(self.request)
                return
            f = self.request.makefile("rwb")
            if greeting:
                f.write(greeting)
                f.flush()
            for expect, reply in steps:
                line = f.readline()
                if not line:
                    return
                if expect is not None and not line.startswith(expect):
                    f.write(b"BAD unexpected\r\n")
                    f.flush()
                    return
                f.write(reply)
                f.flush()

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[0], srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------- environment


@contextlib.contextmanager
def _env(**updates: str | None) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in updates}
    try:
        for k, v in updates.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def temp_home() -> Iterator[Path]:
    """An isolated user home: HOME, USERPROFILE and XDG_CONFIG_HOME point into a fresh directory."""
    with tempfile.TemporaryDirectory(prefix="adamas-testkit-home-") as d:
        home = Path(d)
        (home / ".config").mkdir()
        with _env(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config")):
            yield home


@contextlib.contextmanager
def fake_command(name: str, script: str, *, interpreter: str = "/bin/sh") -> Iterator[Path]:
    """A command ``name`` running ``script`` with the absolute-path ``interpreter``, put in front of the original PATH.

    The rest of PATH stays as it was, so the program under test still finds
    every real tool; ``shutil.which(name)`` returns the fake.
    """
    interp = shutil.which(interpreter) if not os.path.isabs(interpreter) else interpreter
    if not interp or not os.path.isabs(interp):
        raise ValueError(f"interpreter {interpreter!r} must resolve to an absolute path")
    with tempfile.TemporaryDirectory(prefix="adamas-testkit-bin-") as d:
        path = Path(d) / name
        path.write_text(f"#!{interp}\n{script}", encoding="utf-8")
        path.chmod(0o755)
        with _env(PATH=d + os.pathsep + os.environ.get("PATH", "")):
            yield path
