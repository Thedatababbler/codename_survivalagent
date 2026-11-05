# Copied from AdaMAS src/orchestra/codeprojecteval/suite_audit.py (34276d69) for AdaMAS-unit, 2026-11-05. Unchanged.
"""Mechanical audit of an authored suite against the fixed rules (spec §2.4).

Pure functions over the suite's source and the design documents; nothing here
runs tests or opens the held-out directory. The audit produces a list of
violations that the coverage step hands back to the author before custody
freezes the suite, and a list of *soft* cases (error paths the documents
promise without naming the exception class -- the user's rule of 2026-10-04).

Rules:

* ``citation``: every test carries at least one citation comment
  (``# PRD/Architecture/UML/Directory/Design/README ... "sentence"`` or
  ``# DOC: "sentence"``) and every cited sentence, normalised for whitespace
  and punctuation, is a substring of the documents.
* ``private``: no attribute whose name starts with ``_`` on anything (names
  the documents themselves use are exempt), never ``__dict__`` /
  ``__slots__`` / ``vars()``.
* ``error_path``: a ``pytest.raises(X)`` needs a citation that names ``X``;
  a citation that promises an error without a class allows only
  ``pytest.raises(Exception)`` and makes the case soft; no error in the
  citation, no ``raises`` at all.
* ``shim``: no ``sys.modules`` / ``sys.path`` edits, ``importlib.reload``,
  ``importorskip``, import fallbacks, monkeypatching of the project, or a
  suite module named like a project package.
* ``symbol``: every project symbol the suite imports or calls through the
  package is in the allowed set (the lenient public-symbol set), when one is
  given.
* ``import``: module top imports only ``pytest``, ``adamas_testkit`` and the
  standard library every supported interpreter has.
* ``fake_io`` (author fake-object fix, 2026-10-09): no ``monkeypatch.setattr``,
  ``mock.patch`` / ``patch.object`` or assignment replacing a network, process or
  filesystem function of the standard library or a third-party client library
  (``requests.*``, ``urllib.*``, ``socket.*``, ``subprocess.*``, ``shutil.*``,
  ``os.*`` I/O, ...), also when reached through a project module
  (``pkg.vcs.subprocess.run``), unless the replacement is
  ``create_autospec(...)`` / ``Mock(spec=...)`` or the patch passes
  ``autospec=True``.
* ``env_path``: ``PATH`` is set only as a new directory followed by the original
  ``PATH``; ``os.environ`` is never replaced or cleared as a whole.
* ``fake_inject``: no hand-written double class put into a project object or
  module: an instance of a class the suite defines, or the class itself,
  assigned, ``setattr``-ed or monkeypatched onto anything but ``self``
  (``client._imap = ScriptedIMAP()``, ``monkeypatch.setattr(mod, "Client", FakeClient)``).
  Doubles built from the real object, plain mocks and functions are left alone.
* ``interface`` / ``interface_signature`` (2026-10-10): every call into the
  project names a symbol the documents list (``interface``), with arguments
  that fit a signature the documents give (``interface_signature``). A call to
  a listed symbol whose signature the documents do not give is only counted
  (``signatureless_cases``); it no longer makes the case soft (2026-10-11: that
  softened 37% of good cases). The author rule "fewest arguments, no
  undocumented keyword" covers it instead.
* ``call_assert``: no assertion on how a double was called
  (``assert_called_with``, ``call_args``, ``call_count``, ...) unless a cited
  sentence describes the call itself.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

CITATION_RE = re.compile(
    r"#\s*(?:PRD|Architecture|UML|Directory|Design|README|DOC)\b[^\"\n]*\"(?P<quote>[^\"\n]{8,})\"", re.I
)
CITATION_PREFIX_RE = re.compile(r"#\s*(?:PRD|Architecture|UML|Directory|Design|README|DOC)\b", re.I)
EXCEPTION_NAME_RE = re.compile(r"\b([A-Z]\w*(?:Error|Exception|Warning|Exit|Interrupt|Invalid|Failure))\b|\b(KeyError|ValueError|TypeError|StopIteration|Invalid)\b")
ERROR_WORDS_RE = re.compile(r"\b(rais\w*|error\w*|exception\w*|invalid\w*|fail\w*|reject\w*|refus\w*|throw\w*|not (?:allowed|permitted|supported))\b", re.I)
#: stdlib modules that are not on every supported interpreter (added late or removed)
VERSION_DEPENDENT_STDLIB = frozenset({
    "tomllib", "zoneinfo", "graphlib", "distutils", "imp", "asynchat", "asyncore", "smtpd", "cgi", "cgitb",
    "crypt", "nntplib", "pipes", "sndhdr", "spwd", "sunau", "telnetlib", "uu", "xdrlib", "msilib", "nis",
    "ossaudiodev", "audioop", "aifc", "chunk", "mailcap", "imghdr", "lib2to3", "typing_extensions",
})
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))
_ALWAYS_ALLOWED_TOP = frozenset({"pytest", "__future__", "adamas_testkit"})

# network / process / filesystem functions a suite must not replace with a hand-written double
IO_MODULES = frozenset({"requests", "urllib", "urllib3", "httpx", "http", "socket", "ssl", "select", "selectors", "subprocess",
                        "shutil", "smtplib", "ftplib", "imaplib", "poplib", "telnetlib", "asyncio", "aiohttp", "pycurl", "paramiko"})
OS_IO_FUNCS = frozenset({"system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnvp", "fork", "kill",
                         "listdir", "scandir", "walk", "remove", "unlink", "rmdir", "removedirs", "rename", "replace", "makedirs",
                         "mkdir", "open", "stat", "lstat", "chmod", "chown", "access", "getcwd", "chdir", "symlink", "link",
                         "readlink", "path", "environ", "getenv", "putenv", "unsetenv", "urandom"})
ASYNCIO_IO = frozenset({"open_connection", "start_server", "create_subprocess_exec", "create_subprocess_shell", "open_unix_connection"})
SPEC_FACTORIES = frozenset({"create_autospec", "mock.create_autospec", "unittest.mock.create_autospec"})
MOCK_CLASSES = frozenset({"Mock", "MagicMock", "NonCallableMock", "NonCallableMagicMock", "AsyncMock"})
CALL_ASSERTS = frozenset({"assert_called_with", "assert_called_once_with", "assert_any_call", "assert_has_calls", "assert_called",
                          "assert_called_once", "assert_not_called", "assert_awaited_with", "assert_awaited_once_with"})
CALL_ATTRS = frozenset({"call_args", "call_args_list", "call_count", "called", "mock_calls", "method_calls", "await_args"})
CALL_WORDS_RE = re.compile(r"\b(call(?:s|ed|ing)?|invok\w*|pass(?:es|ed|ing)?|argument\w*|parameter\w*|keyword\w*|flag\w*|command\w*|"
                           r"execut\w*|run(?:s|ning)?)\b", re.I)


@dataclass(frozen=True)
class Violation:
    rule: str          # citation | private | error_path | shim | symbol | import | fake_io | fake_inject | env_path | call_assert | interface | interface_signature
    file: str
    case: str          # test function name, "" for module level
    line: int
    detail: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "file": self.file, "case": self.case, "line": self.line, "detail": self.detail}


@dataclass
class AuditResult:
    cases: int = 0
    cited: int = 0
    timeout_marks: int = 0
    violations: list[Violation] = field(default_factory=list)
    soft_cases: list[str] = field(default_factory=list)     # "file::test" whose error path is unnamed
    signatureless_cases: list[str] = field(default_factory=list)  # calls a documented name with no documented signature (counted only)
    citations: dict[str, list[str]] = field(default_factory=dict)  # "file::test" -> normalised quotes

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def cited_ratio(self) -> float:
        return round(self.cited / self.cases, 3) if self.cases else 0.0

    def by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.violations:
            out[v.rule] = out.get(v.rule, 0) + 1
        return dict(sorted(out.items()))

    def to_dict(self) -> dict:
        return {"cases": self.cases, "cited": self.cited, "cited_ratio": self.cited_ratio, "timeout_marks": self.timeout_marks,
                "violations": [v.to_dict() for v in self.violations], "soft_cases": list(self.soft_cases), "by_rule": self.by_rule()}

    def to_author_text(self) -> str:
        """The list handed back to the author: one line per violation, grouped by test."""
        if not self.violations:
            return ""
        lines = ["The suite breaks these fixed rules; fix every item and change nothing else:", ""]
        groups: dict[str, list[Violation]] = {}
        for v in self.violations:
            groups.setdefault(f"{v.file}::{v.case}" if v.case else v.file, []).append(v)
        for key, vs in groups.items():
            lines.append(f"- `{key}`")
            for v in vs:
                lines.append(f"    - [{v.rule}] line {v.line}: {v.detail}")
        return "\n".join(lines)


def normalise(text: str) -> str:
    """Lower-case, drop punctuation and backticks, collapse whitespace."""
    t = text.lower().replace("`", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def _docs_text(docs: Mapping[str, str]) -> str:
    return normalise(" ".join(docs.values()))


def _comment_lines_above(lines: list[str], def_line: int) -> list[str]:
    """Comments directly above a def (decorators in between are skipped)."""
    out = []
    j = def_line - 2  # 0-based index of the line above the def
    while j >= 0 and lines[j].strip().startswith("@"):
        j -= 1
    while j >= 0 and lines[j].strip().startswith("#"):
        out.append(lines[j])
        j -= 1
    return out


def _comment_lines_in(lines: list[str], node: ast.AST) -> list[str]:
    end = getattr(node, "end_lineno", node.lineno)
    return [ln for ln in lines[node.lineno - 1 : end] if ln.strip().startswith("#")]


def _root_name(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    return node.id if isinstance(node, ast.Name) else ""


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _exception_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Tuple):
        return [n for e in node.elts for n in _exception_names(e)]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Name):
        return [node.id]
    return []


def _is_pytest_raises(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "raises" and _root_name(call.func) == "pytest"


def io_target(dotted: str) -> str:
    """The I/O function a dotted patch target names ('' when it names none).

    ``requests.get``, ``socket.socket``, ``os.listdir``, ``asyncio.open_connection``,
    and the same reached through a project module (``pkg.vcs.subprocess.run``).
    """
    parts = [p for p in dotted.split(".") if p]
    for i, p in enumerate(parts):
        rest = parts[i + 1:]
        if p == "os" and rest and rest[0] in OS_IO_FUNCS:
            return ".".join(parts[i:i + 2])
        if p == "asyncio":
            if rest and rest[0] in ASYNCIO_IO:
                return ".".join(parts[i:i + 2])
            continue
        if p in IO_MODULES and (rest or i == len(parts) - 1):
            return ".".join(parts[i:i + 2]) if rest else p
        if p == "builtins" and rest and rest[0] == "open":
            return "builtins.open"
    return ""


def _is_spec_double(node: ast.AST | None) -> bool:
    """create_autospec(...) or Mock(spec=...)/MagicMock(spec_set=...): built from the real object."""
    if not isinstance(node, ast.Call):
        return False
    name = _dotted(node.func)
    if name in SPEC_FACTORIES or name.split(".")[-1] == "create_autospec":
        return True
    if name.split(".")[-1] in MOCK_CLASSES:
        return any(k.arg in ("spec", "spec_set") and not (isinstance(k.value, ast.Constant) and k.value.value is None)
                   for k in node.keywords) or bool(node.args)
    return False


def _refers_to_original_path(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Subscript) and _dotted(n.value) == "os.environ" and isinstance(n.slice, ast.Constant) and n.slice.value == "PATH":
            return True
        if isinstance(n, ast.Call) and _dotted(n.func) in ("os.environ.get", "os.getenv") and n.args and \
                isinstance(n.args[0], ast.Constant) and n.args[0].value == "PATH":
            return True
    return False


def _path_prepends(value: ast.AST, names_bound_to_path: set[str]) -> bool:
    """``new + os.pathsep + os.environ["PATH"]`` (or an f-string / join with the original last)."""
    def original(n: ast.AST) -> bool:
        return _refers_to_original_path(n) or (isinstance(n, ast.Name) and n.id in names_bound_to_path)
    if isinstance(value, ast.BinOp):
        return original(value.right) and not original(value.left)
    if isinstance(value, ast.JoinedStr):
        vals = [v.value for v in value.values if isinstance(v, ast.FormattedValue)]
        return bool(vals) and original(vals[-1]) and not any(original(v) for v in vals[:-1])
    if isinstance(value, ast.Call) and _dotted(value.func).endswith(".join") and value.args and isinstance(value.args[0], (ast.List, ast.Tuple)):
        elts = value.args[0].elts
        return bool(elts) and original(elts[-1]) and not any(original(e) for e in elts[:-1])
    return False


def audit_file(path: Path, docs: Mapping[str, str], *, packages: Iterable[str], allowed_symbols: set[str] | None = None,
               docs_norm: str | None = None, fake_object_audit: bool = True, interface_symbols: set[str] | None = None,
               signatures=None, interface_modules: Mapping[str, str] | None = None) -> AuditResult:
    res = AuditResult()
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    fname = path.name
    pkgs = {p for p in packages if p}
    docs_norm = _docs_text(docs) if docs_norm is None else docs_norm
    docs_raw = " ".join(docs.values())
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        res.violations.append(Violation("syntax", fname, "", exc.lineno or 0, f"file does not parse: {exc.msg}"))
        return res

    # suite module named like a project package
    if path.stem in pkgs or path.stem.split(".")[0] in pkgs:
        res.violations.append(Violation("shim", fname, "", 1, f"suite module named like the project package {path.stem!r}"))

    # module-top imports
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for mod in mods:
                root = mod.split(".")[0]
                if root in _ALWAYS_ALLOWED_TOP:
                    continue
                if root in pkgs:
                    res.violations.append(Violation("import", fname, "", node.lineno, f"module-top import of the project ({mod}); import it inside the test"))
                elif root in VERSION_DEPENDENT_STDLIB:
                    res.violations.append(Violation("import", fname, "", node.lineno, f"module-top import of {mod}, not on every supported interpreter"))
                elif root not in _STDLIB:
                    res.violations.append(Violation("import", fname, "", node.lineno, f"module-top import of a third-party module ({mod})"))
        if isinstance(node, ast.Try):
            for h in node.handlers:
                for n in _exception_names(h.type) if h.type is not None else []:
                    if n in ("ImportError", "ModuleNotFoundError"):
                        res.violations.append(Violation("shim", fname, "", node.lineno, "import fallback (try/except ImportError) at module top"))

    res.timeout_marks += src.count("mark.timeout(")
    # module names the suite imports from the project (``from pkg.mod import X`` / ``import pkg.mod``)
    module_stems: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in pkgs:
            module_stems.update((n.module or "").split("."))
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in pkgs:
                    module_stems.update(a.name.split("."))
    # names the suite defines itself (helpers, stub classes and their methods, fixtures) are not
    # the project's private representation, whatever they are called
    own_names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    own_names.add(t.id)
                elif isinstance(t, ast.Attribute):
                    own_names.add(t.attr)  # self._x = ... inside a stub class

    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    owner = {id(m): c.name for c in tree.body if isinstance(c, ast.ClassDef) for m in c.body}
    for fn in funcs:
        is_test = fn.name.startswith("test")
        case = fn.name if is_test else f"<{fn.name}>"
        # pytest's spelling: file::Class::test for a method, file::test for a function
        key = f"{fname}::{owner[id(fn)]}::{fn.name}" if id(fn) in owner else f"{fname}::{fn.name}"
        quotes: list[str] = []
        raw_quotes: list[str] = []
        if is_test:
            res.cases += 1
            comments = _comment_lines_above(lines, fn.lineno) + _comment_lines_in(lines, fn)
            prefixed = [c for c in comments if CITATION_PREFIX_RE.search(c)]
            for c in comments:
                for m in CITATION_RE.finditer(c):
                    raw_quotes.append(m.group("quote"))
                    quotes.append(normalise(m.group("quote")))
            if not prefixed:
                res.violations.append(Violation("citation", fname, fn.name, fn.lineno, "no citation comment above or inside the test"))
            else:
                if not quotes:
                    res.violations.append(Violation("citation", fname, fn.name, fn.lineno, "citation comment without a quoted sentence"))
                bad = [q for q in quotes if q not in docs_norm]
                if bad:
                    for q in bad[:3]:
                        res.violations.append(Violation("citation", fname, fn.name, fn.lineno, f"cited sentence is not in the documents: \"{q[:80]}\""))
                elif quotes:
                    res.cited += 1
            res.citations[key] = quotes

        for node in ast.walk(fn):
            # private representation
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                a = node.attr
                if a in ("__dict__", "__slots__"):
                    res.violations.append(Violation("private", fname, case, node.lineno, f"accesses {a}"))
                elif not (a.startswith("__") and a.endswith("__")):
                    if a not in docs_raw and a not in own_names:
                        res.violations.append(Violation("private", fname, case, node.lineno, f"accesses private attribute .{a}"))
            if isinstance(node, ast.Call):
                fn_name = _dotted(node.func)
                if fn_name == "vars" and node.args:
                    res.violations.append(Violation("private", fname, case, node.lineno, "vars() reads __dict__"))
                if fn_name in ("getattr", "setattr", "hasattr", "delattr") and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                        and isinstance(node.args[1].value, str) and node.args[1].value.startswith("_") and not node.args[1].value.startswith("__") \
                        and node.args[1].value not in docs_raw and node.args[1].value not in own_names:
                    res.violations.append(Violation("private", fname, case, node.lineno, f"{fn_name}(..., {node.args[1].value!r}) reaches a private name"))
                # shims
                if fn_name in ("importlib.reload", "pytest.importorskip", "__import__", "imp.reload"):
                    res.violations.append(Violation("shim", fname, case, node.lineno, f"{fn_name}() is a shim"))
                if fn_name in ("sys.path.insert", "sys.path.append", "sys.modules.pop", "sys.modules.setdefault", "sys.modules.update"):
                    res.violations.append(Violation("shim", fname, case, node.lineno, f"{fn_name}() edits the interpreter's import state"))
                if fn_name.endswith((".setattr", ".setitem", ".delattr", ".delitem")) and node.args:
                    target = node.args[0]
                    tname = _dotted(target) if not isinstance(target, ast.Constant) else str(target.value)
                    if tname.split(".")[0] in pkgs:
                        res.violations.append(Violation("shim", fname, case, node.lineno, f"monkeypatches the project ({tname})"))
                if fn_name in ("mock.patch", "unittest.mock.patch", "patch", "mock.patch.object") and node.args and isinstance(node.args[0], ast.Constant) \
                        and str(node.args[0].value).split(".")[0] in pkgs:
                    res.violations.append(Violation("shim", fname, case, node.lineno, f"patches the project ({node.args[0].value})"))
                # project symbols called through the package
                if allowed_symbols is not None and fn_name and fn_name.split(".")[0] in pkgs and "." in fn_name:
                    parts = fn_name.split(".")
                    leaf = parts[-1]
                    # pkg.func / pkg.mod.func / pkg.Class / pkg.mod.Class.method are checkable; a chain
                    # through an attribute the documents do not name (an instance, __version__) is not
                    middle = parts[1:-1]
                    checkable = not any(m.startswith("_") for m in parts) and all(
                        m in allowed_symbols or m in module_stems for m in middle)
                    if checkable and leaf not in allowed_symbols and fn_name not in allowed_symbols \
                            and not (middle and f"{middle[-1]}.{leaf}" in allowed_symbols):
                        res.violations.append(Violation("symbol", fname, case, node.lineno, f"calls {fn_name}, which the documents do not list"))
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, ast.Assign) else ([node.target] if isinstance(node, ast.AugAssign) else node.targets)
                for t in targets:
                    d = _dotted(t.value) if isinstance(t, ast.Subscript) else _dotted(t)
                    if d in ("sys.modules", "sys.path"):
                        res.violations.append(Violation("shim", fname, case, node.lineno, f"writes {d}"))
            if isinstance(node, ast.Try):
                for h in node.handlers:
                    for n in _exception_names(h.type) if h.type is not None else []:
                        if n in ("ImportError", "ModuleNotFoundError"):
                            res.violations.append(Violation("shim", fname, case, node.lineno, "import fallback (try/except ImportError)"))
            if isinstance(node, ast.ImportFrom) and allowed_symbols is not None and (node.module or "").split(".")[0] in pkgs:
                for a in node.names:
                    if a.name != "*" and a.name not in allowed_symbols and not a.name.startswith("_"):
                        res.violations.append(Violation("symbol", fname, case, node.lineno, f"imports {node.module}.{a.name}, which the documents do not list"))
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in pkgs and not is_test and fn.name not in ("fixture",):
                        pass  # imports inside fixtures/helpers are fine

        # error paths
        if is_test:
            raises = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _is_pytest_raises(n)]
            if raises:
                cited_text = " ".join(raw_quotes)
                cited_classes = {m.group(1) or m.group(2) for m in EXCEPTION_NAME_RE.finditer(cited_text)}
                for call in raises:
                    names = _exception_names(call.args[0]) if call.args else []
                    # a class the cited sentence spells out counts whatever it is called
                    cited_classes |= {n for n in names if re.search(rf"\b{re.escape(n)}\b", cited_text)}
                    if cited_classes:
                        missing = [n for n in names if n not in cited_classes and n not in cited_text]
                        if missing:
                            res.violations.append(Violation("error_path", fname, fn.name, call.lineno,
                                                            f"pytest.raises({', '.join(missing)}) but the cited sentence names {sorted(cited_classes)}"))
                    elif ERROR_WORDS_RE.search(cited_text):
                        if all(n in ("Exception", "BaseException") for n in names):
                            if key not in res.soft_cases:
                                res.soft_cases.append(key)
                        else:
                            res.violations.append(Violation("error_path", fname, fn.name, call.lineno,
                                                            f"pytest.raises({', '.join(names)}) but the cited sentence names no exception class; use pytest.raises(Exception)"))
                    else:
                        res.violations.append(Violation("error_path", fname, fn.name, call.lineno,
                                                        "pytest.raises in a test whose cited sentence mentions no error"))
    if fake_object_audit:
        res.violations.extend(fake_object_violations(tree, fname, res.citations))
    if interface_symbols is not None:
        iv, isoft = interface_check(tree, fname, packages=pkgs, documented=interface_symbols, signatures=signatures,
                                    modules=interface_modules)
        res.violations.extend(iv)
        res.signatureless_cases.extend(isoft)    # counted only; no longer made soft (user decision 2026-10-11)
    return res


def fake_object_violations(tree: ast.AST, fname: str, citations: Mapping[str, list[str]]) -> list[Violation]:
    """The ``fake_io``, ``env_path`` and ``call_assert`` rules over one parsed suite file."""
    out: list[Violation] = []
    funcs = sorted((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                   key=lambda f: (f.lineno, -(f.end_lineno or f.lineno)))

    def case_of(line: int) -> str:
        best = ""
        for f in funcs:
            if f.lineno <= line <= (f.end_lineno or f.lineno) or any(d.lineno == line for d in f.decorator_list):
                best = f.name if f.name.startswith("test") else f"<{f.name}>"
        return best

    def quotes_of(case: str) -> str:
        return " ".join(q for k, qs in citations.items() if k.split("::")[-1] == case for q in qs)

    suite_classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    bound_to_double: set[str] = set()      # names assigned an instance of a suite class
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            cn = _dotted(n.value.func).split(".")[-1]
            if cn in suite_classes:
                bound_to_double.update(t.id for t in n.targets if isinstance(t, ast.Name))

    def double_kind(value: ast.AST | None, private: bool) -> str:
        """Only a hand-written double class counts (2026-10-10): an instance of a class the suite defines, or the
        class itself. Doubles generated from the real object (create_autospec, Mock(spec=...)) and plain mocks,
        functions or lambdas do not."""
        if value is None:
            return ""
        if isinstance(value, ast.Call):
            cn = _dotted(value.func).split(".")[-1]
            if cn in suite_classes:
                return f"an instance of the suite's own class {cn}"
        if isinstance(value, ast.Name):
            if value.id in bound_to_double:
                return f"the hand-written double {value.id}"
            if value.id in suite_classes:
                return f"the suite's own class {value.id}"
        return ""

    path_names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and _refers_to_original_path(n.value):
            path_names.update(t.id for t in n.targets if isinstance(t, ast.Name))

    for n in ast.walk(tree):
        line = getattr(n, "lineno", 0)
        if isinstance(n, ast.Call):
            name = _dotted(n.func)
            leaf = name.split(".")[-1]
            # monkeypatch.setattr("mod.attr", new) / monkeypatch.setattr(obj, "attr", new)
            if leaf == "setattr" and name != "setattr" and n.args:
                if isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    target, new = n.args[0].value, (n.args[1] if len(n.args) > 1 else None)
                else:
                    attr = n.args[1].value if len(n.args) > 1 and isinstance(n.args[1], ast.Constant) else ""
                    target, new = f"{_dotted(n.args[0])}.{attr}", (n.args[2] if len(n.args) > 2 else None)
                    if _dotted(n.args[0]) == "os" and attr == "environ":
                        out.append(Violation("env_path", fname, case_of(line), line, "replaces os.environ as a whole"))
                        continue
                io = io_target(target)
                if io and not _is_spec_double(new):
                    out.append(Violation("fake_io", fname, case_of(line), line,
                                         f"replaces {io} with a hand-written double; use the testkit's local environment, or create_autospec / Mock(spec=...)"))
                elif not io and not (target.endswith(".environ") or target == "os.environ"):
                    kind = double_kind(new, True)
                    if kind:
                        out.append(Violation("fake_inject", fname, case_of(line), line,
                                             f"replaces {target} with {kind}; a double must be create_autospec / Mock(spec=...) of the real object"))
            if name == "setattr" and len(n.args) >= 3 and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str) \
                    and _root_name(n.args[0]) not in ("self", "cls", ""):
                kind = double_kind(n.args[2], n.args[1].value.startswith("_"))
                if kind:
                    out.append(Violation("fake_inject", fname, case_of(line), line,
                                         f"puts {kind} into {_dotted(n.args[0])}.{n.args[1].value}"))
            # mock.patch("mod.attr", ...) / patch.object(obj, "attr", ...)
            if leaf in ("patch", "object") and ("patch" in name):
                if leaf == "object" and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant):
                    target, new = f"{_dotted(n.args[0])}.{n.args[1].value}", (n.args[2] if len(n.args) > 2 else None)
                elif leaf == "patch" and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    target, new = n.args[0].value, (n.args[1] if len(n.args) > 1 else None)
                else:
                    target, new = "", None
                kw = {k.arg: k.value for k in n.keywords}
                new = kw.get("new", new)
                autospec = isinstance(kw.get("autospec"), ast.Constant) and kw["autospec"].value is True
                spec_kw = any(k in kw for k in ("spec", "spec_set", "new_callable")) and not isinstance(kw.get("new_callable"), ast.Name)
                io = io_target(target)
                if io and not (autospec or spec_kw or _is_spec_double(new)):
                    out.append(Violation("fake_io", fname, case_of(line), line,
                                         f"patches {io} without autospec; use the testkit's local environment, or autospec=True / create_autospec"))
                elif target and not io and not (autospec or spec_kw or _is_spec_double(new)):
                    kind = double_kind(new, True)
                    if kind:
                        out.append(Violation("fake_inject", fname, case_of(line), line,
                                             f"patches {target} with {kind}; use autospec=True or create_autospec"))
            # PATH set through monkeypatch.setenv / setitem
            if leaf in ("setenv", "setitem") and n.args:
                key_i = 0 if leaf == "setenv" else 1
                if leaf == "setitem" and _dotted(n.args[0]) != "os.environ":
                    key_i = -1
                if key_i >= 0 and len(n.args) > key_i + 1 and isinstance(n.args[key_i], ast.Constant) and n.args[key_i].value == "PATH":
                    if not _path_prepends(n.args[key_i + 1], path_names):
                        out.append(Violation("env_path", fname, case_of(line), line,
                                             "sets PATH without keeping the original after the new directory (new_dir + os.pathsep + os.environ['PATH'])"))
            if name in ("os.environ.clear",):
                out.append(Violation("env_path", fname, case_of(line), line, "clears os.environ"))
            # assertions on how a double was called
            if leaf in CALL_ASSERTS and isinstance(n.func, ast.Attribute):
                case = case_of(line)
                if not CALL_WORDS_RE.search(quotes_of(case)):
                    out.append(Violation("call_assert", fname, case, line,
                                         f"asserts how a double was called (.{leaf}) but no cited sentence describes the call"))
        elif isinstance(n, ast.Attribute) and n.attr in CALL_ATTRS and isinstance(n.ctx, ast.Load):
            parent_assert = any(isinstance(a, ast.Assert) and a.lineno <= line <= (a.end_lineno or a.lineno) for a in ast.walk(tree))
            if parent_assert:
                case = case_of(line)
                if not CALL_WORDS_RE.search(quotes_of(case)):
                    out.append(Violation("call_assert", fname, case, line,
                                         f"asserts on .{n.attr} of a double but no cited sentence describes the call"))
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                d = _dotted(t.value) if isinstance(t, ast.Subscript) else _dotted(t)
                if isinstance(t, ast.Subscript) and d == "os.environ" and isinstance(t.slice, ast.Constant) and t.slice.value == "PATH":
                    if not _path_prepends(n.value, path_names):
                        out.append(Violation("env_path", fname, case_of(line), line, "assigns os.environ['PATH'] without keeping the original after the new directory"))
                elif d == "os.environ" and not isinstance(t, ast.Subscript):
                    out.append(Violation("env_path", fname, case_of(line), line, "replaces os.environ as a whole"))
                elif isinstance(t, ast.Attribute):
                    io = io_target(d)
                    if io and not _is_spec_double(n.value):
                        out.append(Violation("fake_io", fname, case_of(line), line, f"assigns a hand-written double to {io}"))
                    elif _root_name(t) not in ("self", "cls", "") and not (t.attr.startswith("__") and t.attr.endswith("__")):
                        kind = double_kind(n.value, t.attr.startswith("_"))
                        if kind:
                            out.append(Violation("fake_inject", fname, case_of(line), line,
                                                 f"puts {kind} into {d}; test through the documented interface and the testkit's local environment"))
    # one entry per (case, rule, line)
    seen, uniq = set(), []
    for v in out:
        k = (v.case, v.rule, v.line)
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return uniq


def _import_violations(tree: ast.AST, fname: str, pkgs: set[str], documented: set[str], leaves: set[str],
                       modules: Mapping[str, str], case_of) -> list[Violation]:
    """``interface``: a project import of a name the documents do not list, or from a module other than the one they name."""
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.ImportFrom) or not n.module or n.module.split(".")[0] not in pkgs:
            continue
        mod_path = n.module.replace(".", "/")
        for a in n.names:
            if a.name == "*" or a.name.startswith("_"):
                continue
            if a.name not in documented and a.name not in leaves:
                # a module imported by name is fine when the documents name it as a file
                if not any(str(m).removesuffix(".py").removesuffix("/__init__").endswith(f"{mod_path}/{a.name}")
                           for ms in modules.values() for m in ([ms] if isinstance(ms, str) else ms)):
                    out.append(Violation("interface", fname, case_of(n.lineno), n.lineno, f"imports {n.module}.{a.name}, which the documents do not list"))
                continue
            wheres = modules.get(a.name) or ()
            wheres = [wheres] if isinstance(wheres, str) else list(wheres)

            def matches(w: str) -> bool:
                w = w.removesuffix(".py").removesuffix("/__init__")
                tail = lambda x: x.split("/", 1)[1] if "/" in x else x  # noqa: E731
                # documents may prefix the repository directory, or root the tree at the repository name
                return w == mod_path or w.endswith("/" + mod_path) or tail(w) == tail(mod_path)
            if wheres and "." in n.module and not any(matches(w) for w in wheres):
                out.append(Violation("interface", fname, case_of(n.lineno), n.lineno,
                                     f"imports {a.name} from {n.module}, but the documents place it in {wheres[0]}"))
    return out


def interface_check(tree: ast.AST, fname: str, *, packages: Iterable[str], documented: set[str], signatures,
                    modules: Mapping[str, str] | None = None) -> tuple[list[Violation], list[str]]:
    """``interface`` / ``interface_signature`` violations and the soft cases ("file::[Cls::]test") of one suite file.

    Calls into the project are the calls of names imported from the project
    package, of attributes reached through a project module, and of methods on
    variables a project call returned. ``documented`` holds the documents'
    public symbols (``Class.method`` and plain names); ``signatures`` is a
    ``doc_signatures.SignatureIndex``.
    """
    pkgs = {p for p in packages if p}
    leaves = {n.split(".")[-1] for n in documented}
    out: list[Violation] = []
    soft: list[str] = []
    owner = {id(m): c.name for c in getattr(tree, "body", []) if isinstance(c, ast.ClassDef) for m in c.body}
    module_imports: dict[str, str] = {}
    for n in getattr(tree, "body", []):
        _collect_imports(n, pkgs, module_imports)
    funcs = sorted((x for x in ast.walk(tree) if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))), key=lambda f: f.lineno)

    def case_of(line: int) -> str:
        best = ""
        for f in funcs:
            if f.lineno <= line <= (f.end_lineno or f.lineno):
                best = f.name if f.name.startswith("test") else f"<{f.name}>"
        return best

    out.extend(_import_violations(tree, fname, pkgs, documented, leaves, modules or {}, case_of))
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]:
        key = f"{fname}::{owner[id(fn)]}::{fn.name}" if id(fn) in owner else f"{fname}::{fn.name}"
        names = dict(module_imports)          # local name -> project dotted name
        for n in ast.walk(fn):
            _collect_imports(n, pkgs, names)
        instances: dict[str, str] = {}        # variable -> project class / call it came from
        is_soft = False
        for n in sorted((x for x in ast.walk(fn) if isinstance(x, (ast.Assign, ast.Call, ast.With))), key=lambda x: (x.lineno, x.col_offset)):
            if isinstance(n, ast.Assign):
                if isinstance(n.value, ast.Call):
                    target = _project_callee(n.value.func, names, instances)
                    if target and target.split(".")[-1][:1].isupper():   # an instance of a project class
                        for t in n.targets:
                            if isinstance(t, ast.Name):
                                instances[t.id] = target
                continue                      # the call on the right is visited as its own node
            if isinstance(n, ast.With):
                for item in n.items:
                    if isinstance(item.context_expr, ast.Call) and isinstance(item.optional_vars, ast.Name):
                        target = _project_callee(item.context_expr.func, names, instances)
                        if target and target.split(".")[-1][:1].isupper():
                            instances[item.optional_vars.id] = target
                continue
            callee = _project_callee(n.func, names, instances)
            if not callee:
                continue
            leaf = callee.split(".")[-1]
            if leaf.startswith("_"):
                continue                      # the private rule already covers it
            qualified = [callee, ".".join(callee.split(".")[-2:])]
            on_instance = isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id in instances
            parts = callee.split(".")
            # an attribute of a documented class (Model.objects.filter, View.as_view) or a method on a project
            # object may come from a third-party base class the documents do not repeat
            through_class = any(p[:1].isupper() for p in parts[:-1])
            if not on_instance and not through_class and not any(q in documented for q in qualified) and leaf not in leaves:
                out.append(Violation("interface", fname, fn.name, n.lineno, f"calls {callee}, which the documents do not list"))
                continue
            sigs = signatures.lookup(".".join(callee.split(".")[-2:])) if signatures is not None else []
            if not sigs and signatures is not None:
                sigs = signatures.lookup(leaf)
            if not sigs:
                is_soft = True
                continue
            star_args = any(isinstance(a, ast.Starred) for a in n.args)
            star_kw = any(k.arg is None for k in n.keywords)
            n_pos = sum(1 for a in n.args if not isinstance(a, ast.Starred))
            kws = [k.arg for k in n.keywords if k.arg is not None]
            verdicts = [sg.accepts(n_pos, kws, star_args=star_args, star_kw=star_kw) for sg in sigs]
            if not any(ok for ok, _ in verdicts):
                out.append(Violation("interface_signature", fname, fn.name, n.lineno, f"{callee}(...): {verdicts[0][1]}"))
        if is_soft:
            soft.append(key)
    return out, soft


def _collect_imports(node: ast.AST, pkgs: set[str], names: dict[str, str]) -> None:
    if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in pkgs:
        for a in node.names:
            if a.name != "*":
                names[a.asname or a.name] = f"{node.module}.{a.name}"
    elif isinstance(node, ast.Import):
        for a in node.names:
            if a.name.split(".")[0] in pkgs:
                names[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]


def _project_callee(func: ast.AST, names: Mapping[str, str], instances: Mapping[str, str]) -> str:
    """The project dotted name a call reaches ('' when it is not a project call)."""
    if isinstance(func, ast.Name):
        return names.get(func.id, "")
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            if base.id in instances:
                return f"{instances[base.id].split('.')[-1]}.{func.attr}"
            if base.id in names:
                return f"{names[base.id]}.{func.attr}"
        d = _dotted(func)
        root = d.split(".")[0]
        if root in names:
            return names[root] + d[len(root):]
    return ""


def audit_suite(suite_dir: str | Path, docs: Mapping[str, str], *, packages: Iterable[str],
                allowed_symbols: set[str] | None = None, fake_object_audit: bool = True, interface: bool = False) -> AuditResult:
    """Audit every ``*.py`` under ``suite_dir`` (recursively) and merge the results."""
    root = Path(suite_dir)
    total = AuditResult()
    docs_norm = _docs_text(docs)
    pkgs = list(packages)
    iface_syms, sigs, iface_mods = None, None, None
    if interface:
        from orchestra.codeprojecteval.doc_signatures import derive_signatures
        from orchestra.codeprojecteval.public_symbols import derive_public_symbols

        inv = derive_public_symbols(docs)
        iface_syms = inv.names() | (allowed_symbols or set())
        iface_mods: dict[str, set[str]] = {}
        for sym in inv.symbols:
            if sym.module:
                iface_mods.setdefault(sym.name, set()).add(sym.module)
        sigs = derive_signatures(docs)
        iface_syms |= set(sigs.by_name)
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        r = audit_file(p, docs, packages=pkgs, allowed_symbols=allowed_symbols, docs_norm=docs_norm,
                       fake_object_audit=fake_object_audit, interface_symbols=iface_syms, signatures=sigs,
                       interface_modules=iface_mods)
        total.cases += r.cases
        total.cited += r.cited
        total.timeout_marks += r.timeout_marks
        total.violations.extend(r.violations)
        total.soft_cases.extend(r.soft_cases)
        total.signatureless_cases.extend(r.signatureless_cases)
        total.citations.update(r.citations)
    # package-shaped directories inside the suite
    for d in root.rglob("*"):
        if d.is_dir() and d.name in set(pkgs):
            total.violations.append(Violation("shim", str(d.relative_to(root)), "", 0, f"directory named like the project package {d.name!r}"))
    total.violations.sort(key=lambda v: (v.file, v.line, v.rule))
    return total


__all__ = ["AuditResult", "CITATION_RE", "fake_object_violations", "interface_check", "io_target", "VERSION_DEPENDENT_STDLIB", "Violation", "audit_file", "audit_suite", "normalise"]
