# Copied from AdaMAS src/orchestra/codeprojecteval/public_symbols.py (34276d69) for AdaMAS-unit, 2026-11-05. Unchanged.
"""Public symbols a milestone owns, derived from the design documents alone.

The milestone plans carry focus paths but no symbol list, and the author
audit, the behaviour inventory and the symbol-level coverage metric all need
one (author-evolution spec §2.1, §2.4, §6). The rule, fixed by the user on
2026-10-04:

* a name counts when it appears in identifier form in the documents (a UML
  class block, a code block, backticks, the architecture document's class and
  function lists), does not start with an underscore, and its module falls
  under the milestone's focus paths;
* the module is the one the documents state (the architecture document lists
  symbols under ``file.py`` headings); failing that, the directory tree's
  file whose stem matches the name;
* methods are recorded as ``Class.method`` and belong with their class;
* names whose module cannot be determined go to an *unattributed* list. The
  audit accepts them (lenient); coverage and metrics ignore them (strict).

Only the documents are read. Nothing here opens the held-out suite or the
reference implementation.
"""

from __future__ import annotations

import builtins
import keyword
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

_TREE_LINE = re.compile(r"^(?P<indent>[\s│|]*)(?:├──|└──|\+--|\\--|--)\s*(?P<name>[^\s#]+)")
# `file.py` :  /  ## `pkg/file.py`  /  `__init__.py` : This module serves as ... (text on the same line)
_MODULE_HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?`(?P<path>/?[\w./-]+\.py)`\s*:?(?:\s|$)")
_BULLET = re.compile(r"^(?P<indent>\s*)[-*]\s+(?:The\s+(?:instance|class)\s+attribute\s+)?`(?P<code>[^`]+)`")
_UML_CLASS = re.compile(r"^\s*class\s+(?P<name>[A-Za-z_]\w*)\s*(?:\{|$)")
_UML_MEMBER = re.compile(r"^\s*(?P<vis>[+\-#~]?)\s*(?P<name>[A-Za-z_]\w*)\s*(?P<call>\()?")
_BACKTICK = re.compile(r"`([A-Za-z_][\w.]*)(?:\([^`]*\))?`")
_IMPORT_IN_DOC = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+([\w, ]+)", re.M)
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_BUILTIN_NAMES = set(dir(builtins)) | set(keyword.kwlist) | {"self", "cls", "None", "True", "False"}
_COMMON_NOISE = {"dict", "list", "str", "int", "bool", "bytes", "float", "set", "tuple", "object",
                 "json", "pytest", "python", "pip", "git", "setup.py", "README", "requirements.txt"}


@dataclass(frozen=True)
class DocumentedSymbol:
    name: str          # "Table", "Table.insert", "deprecated"
    module: str        # "tinydb/table.py"; "" when unattributed
    source: str        # which document named it


@dataclass
class SymbolInventory:
    symbols: tuple[DocumentedSymbol, ...]
    tree: tuple[str, ...] = field(default_factory=tuple)

    def names(self) -> set[str]:
        return {s.name for s in self.symbols}

    def unattributed(self) -> set[str]:
        return {s.name for s in self.symbols if not s.module}

    def owned_by(self, focus_paths: Iterable[str]) -> set[str]:
        """Strict: symbols whose module falls under one of ``focus_paths``."""
        focus = [_norm_path(p) for p in focus_paths]
        out = set()
        for s in self.symbols:
            if s.module and any(_under(s.module, f) for f in focus):
                out.add(s.name)
        return out

    def lenient(self, focus_paths: Iterable[str], predecessor_paths: Iterable[str] = ()) -> set[str]:
        """Audit set: own + predecessors' + unattributed, plus bare class names of methods."""
        names = self.owned_by(focus_paths) | self.owned_by(predecessor_paths) | self.unattributed()
        # bare heads (the class of a method) and bare leaves (a module-as-class member in a UML
        # diagram, ``adjacency_graphs.ADJACENCY_GRAPHS``) are accepted by the audit too
        return names | {n.split(".")[0] for n in names} | {n.split(".")[-1] for n in names}

    def module_of(self, name: str) -> str:
        for s in self.symbols:
            if s.name == name and s.module:
                return s.module
        return ""


def _norm_path(p: str) -> str:
    p = str(p).strip().replace("\\", "/")
    p = re.sub(r"^(\./|/)", "", p)
    return p.rstrip("/")


def _under(module: str, focus: str) -> bool:
    m, f = _norm_path(module), _norm_path(focus)
    if not f:
        return False
    if m == f or m.startswith(f + "/"):
        return True
    # focus given as a bare filename or as a path without the package prefix
    if m.endswith("/" + f) or (f.endswith(".py") and f.count("/") == 0 and m.rsplit("/", 1)[-1] == f):
        return True
    # the documents' tree is rooted at the repository name while the plan's focus paths are
    # rooted at the package (djangorestframework-simplejwt/tokens.py vs rest_framework_simplejwt/tokens.py)
    mt, ft = m.split("/", 1), f.split("/", 1)
    if len(mt) == 2 and len(ft) == 2:
        return mt[1] == ft[1] or mt[1].startswith(ft[1] + "/")
    return False


def parse_directory_tree(text: str) -> list[str]:
    """``├──``-style tree -> relative file paths (directories end with '/')."""
    stack: list[tuple[int, str]] = []
    out: list[str] = []
    for raw in text.splitlines():
        m = _TREE_LINE.match(raw)
        if not m:
            continue
        depth = len(m.group("indent").replace("│", " ").replace("|", " ")) // 4
        name = m.group("name").rstrip("/")
        while stack and stack[-1][0] >= depth:
            stack.pop()
        path = "/".join([s[1] for s in stack] + [name])
        is_dir = "." not in name or name.startswith(".")  # 'tinydb', '.github'
        if is_dir and not name.endswith((".py", ".txt", ".md", ".cfg", ".toml", ".ini", ".json", ".yaml", ".yml")):
            stack.append((depth, name))
            out.append(path + "/")
        else:
            out.append(path)
    return out


def _resolve_module(stem_or_path: str, tree: Iterable[str]) -> str:
    """'database.py' or 'tinydb/database.py' -> the tree path that ends with it."""
    cand = _norm_path(stem_or_path)
    files = [t for t in tree if t.endswith(".py")]
    exact = [t for t in files if t == cand or t.endswith("/" + cand)]
    if len(exact) == 1:
        return exact[0]
    if exact:
        # prefer the shallowest (a package module over a tests copy)
        return sorted(exact, key=lambda t: t.count("/"))[0]
    if cand.endswith(".py") and files:
        import difflib
        stem = cand.rsplit("/", 1)[-1][:-3]
        stems = {t.rsplit("/", 1)[-1][:-3]: t for t in files if not t.rsplit("/", 1)[-1].startswith("__")}
        close = difflib.get_close_matches(stem, list(stems), n=1, cutoff=0.85)
        if close:
            return stems[close[0]]
    return cand if cand.endswith(".py") else ""


def _module_for_name(name: str, tree: Iterable[str]) -> str:
    """Filename match: ``LRUCache`` -> ``utils.py``? no; ``Table`` -> ``table.py`` yes."""
    base = name.split(".")[0]
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", base).lower()
    for t in tree:
        if not t.endswith(".py"):
            continue
        stem = t.rsplit("/", 1)[-1][:-3]
        if stem in (base.lower(), snake):
            return t
    return ""


def _code_head(code: str) -> str:
    """'table(name: str, **kwargs) -> Table' -> 'table'; 'rsa.PublicKey' -> 'rsa.PublicKey'."""
    code = code.strip()
    m = re.match(r"([A-Za-z_][\w.]*)", code)
    return m.group(1) if m else ""


def _is_identifier_like(tok: str) -> bool:
    parts = tok.split(".")
    return all(_IDENT.match(p) for p in parts) and tok not in _BUILTIN_NAMES and tok not in _COMMON_NOISE


def derive_public_symbols(docs: Mapping[str, str]) -> SymbolInventory:
    """Documents (filename -> text) -> inventory. Reads nothing else."""
    tree: list[str] = []
    for fname, text in docs.items():
        if "directory_tree" in fname or "architecture" in fname.lower():
            tree.extend(parse_directory_tree(text))
    tree = sorted(set(tree))
    found: dict[tuple[str, str], DocumentedSymbol] = {}
    by_name: dict[str, DocumentedSymbol] = {}

    def add(name: str, module: str, source: str, *, explicit: bool = False) -> None:
        """``explicit``: listed under a module heading, so even a builtin-looking
        name such as the ``set`` operation counts."""
        if not name or name.split(".")[-1].startswith("_") or name.split(".")[0].startswith("_"):
            return
        parts = name.split(".")
        if not all(_IDENT.match(p) for p in parts):
            return
        if not explicit and not _is_identifier_like(name):
            return
        if not module and name in by_name:
            return  # an attributed entry already exists; an unattributed sighting adds nothing
        if (name, module) in found:
            return
        if not module:
            found[(name, "")] = DocumentedSymbol(name, "", source)
        else:
            found.pop((name, ""), None)
            found[(name, module)] = DocumentedSymbol(name, module, source)
        if module or name not in by_name:
            by_name[name] = found[(name, module)]

    # 1. architecture document: `file.py` headings with bullet lists
    for fname, text in docs.items():
        if "architecture" not in fname.lower() and "design" not in fname.lower():
            continue
        module = ""
        current_class = ""
        class_indent = -1
        for line in text.splitlines():
            h = _MODULE_HEADING.match(line)
            if h:
                module = _resolve_module(h.group("path"), tree)
                current_class, class_indent = "", -1
                continue
            b = _BULLET.match(line)
            if not b or not module:
                continue
            head = _code_head(b.group("code"))
            if not head:
                continue
            indent = len(b.group("indent"))
            stated = re.search(r"\(\s*from\s+`([\w.]+)`\s*\)", line[b.end():])
            if stated:
                explicit_mod = _resolve_module(stated.group(1).replace(".", "/") + ".py", tree) or \
                    _resolve_module(stated.group(1).replace(".", "/") + "/__init__.py", tree)
                if explicit_mod:
                    add(head, explicit_mod, fname, explicit=True)
            is_attr = "attribute" in line[: b.start("code")]
            if indent == 0 or class_indent < 0 or indent <= class_indent:
                # top-level symbol of the module
                add(head, module, fname, explicit=True)
                current_class, class_indent = (head, indent) if head[:1].isupper() else ("", -1)
            else:
                if current_class:
                    add(f"{current_class}.{head}", module, fname, explicit=True)
                else:
                    add(head, module, fname, explicit=True)

    # 2. UML class diagrams (mermaid classDiagram blocks)
    for fname, text in docs.items():
        if "uml" not in fname.lower():
            continue
        cls = ""
        for line in text.splitlines():
            c = _UML_CLASS.match(line)
            if c:
                cls = c.group("name")
                module = by_name[cls].module if cls in by_name and by_name[cls].module else _module_for_name(cls, tree)
                add(cls, module, fname, explicit=True)
                continue
            if line.strip() == "}":
                cls = ""
                continue
            if not cls:
                continue
            m = _UML_MEMBER.match(line)
            if not m or m.group("vis") == "-":
                continue
            name = m.group("name")
            if name in ("class",) or name.startswith("_"):
                continue
            module = by_name[cls].module if cls in by_name else ""
            add(f"{cls}.{name}", module, fname, explicit=True)

    # 3. import statements in code blocks: package-level API
    for fname, text in docs.items():
        for mod, names in _IMPORT_IN_DOC.findall(text):
            root = mod.split(".")[0]
            pkg_init = _resolve_module(mod.replace(".", "/") + "/__init__.py", tree) or _resolve_module(mod.replace(".", "/") + ".py", tree)
            for n in names.split(","):
                n = n.strip().split(" as ")[0].strip()
                if n and n != "*":
                    add(n, pkg_init, fname)
            del root

    # 4. backticked identifiers anywhere: attributed by filename match, else unattributed
    for fname, text in docs.items():
        for tok in _BACKTICK.findall(text):
            tok = tok.rstrip(".")
            if tok in by_name or tok.endswith(".py") or not _is_identifier_like(tok):
                continue
            if tok.lower() in {t.rsplit("/", 1)[-1][:-3] for t in tree if t.endswith(".py")}:
                continue  # a module name, not a symbol
            module = _module_for_name(tok, tree) if "." not in tok else ""
            if "." in tok:
                head = tok.split(".")[0]
                if head in by_name and by_name[head].module:
                    module = by_name[head].module
                elif head.lower() in {t.rsplit("/", 1)[-1][:-3] for t in tree if t.endswith(".py")} or any(t.rstrip("/").split("/")[-1] == head for t in tree if t.endswith("/")):
                    module = _resolve_module(tok.replace(".", "/").rsplit("/", 1)[0] + ".py", tree)
                    tok = tok.split(".")[-1]
            add(tok, module, fname)

    return SymbolInventory(symbols=tuple(sorted(found.values(), key=lambda s: (s.module, s.name))), tree=tuple(tree))


def load_docs(docs_dir: str | Path) -> dict[str, str]:
    out = {}
    for p in sorted(Path(docs_dir).glob("*")):
        if p.is_file() and p.suffix.lower() in (".md", ".txt", ".rst"):
            out[p.name] = p.read_text(encoding="utf-8", errors="replace")
    return out


__all__ = ["DocumentedSymbol", "SymbolInventory", "derive_public_symbols", "load_docs", "parse_directory_tree"]
