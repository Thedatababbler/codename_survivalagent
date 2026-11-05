# Copied from AdaMAS src/orchestra/codeprojecteval/doc_signatures.py (34276d69) for AdaMAS-unit, 2026-11-05. Unchanged.
"""Call signatures the design documents state explicitly (interface check, 2026-10-10).

The author may call only symbols the documents list, with arguments that fit
the signature the documents give. This module extracts those signatures:
- backticked calls: `` `insert(document)` ``, `` `Table.insert(document, doc_id=None)` ``,
  `` `IMAPClient(host, port=None, ssl=True)` ``;
- UML member lines: ``+ search(criteria='ALL', charset=None) list``;
- ``def`` lines in code blocks.

Each signature is parsed with :mod:`ast` as ``def f(<params>): pass``. Type
annotations stay when they parse; otherwise they are stripped and the names
kept. A name the documents mention without parentheses has no signature,
which is not the same as an empty one.

Only the documents are read.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

_BACKTICK_CALL = re.compile(r"`(?P<name>[A-Za-z_][\w.]*)\s*\((?P<args>[^`]*)\)(?:\s*->\s*[^`]*)?`")
_UML_MEMBER = re.compile(r"^\s*[+\-#~]?\s*(?:async\s+)?(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^()]*(?:\([^()]*\)[^()]*)*)\)")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<args>.*)\)\s*(?:->[^:]*)?:", re.M)
_UML_CLASS = re.compile(r"^\s*class\s+(?P<name>[A-Za-z_]\w*)")


@dataclass(frozen=True)
class Signature:
    params: tuple[str, ...]            # positional-or-keyword names, in order (self/cls dropped)
    required: frozenset[str]           # names without a default
    kwonly: tuple[str, ...] = ()
    varargs: bool = False
    varkw: bool = False
    posonly: int = 0
    source: str = ""
    defaults_known: bool = True        # False: the documents list the parameters without any default (UML style)

    def accepts(self, n_positional: int, keywords: Iterable[str], *, star_args: bool = False, star_kw: bool = False) -> tuple[bool, str]:
        kws = list(keywords)
        if n_positional > len(self.params) and not self.varargs and not star_args:
            return False, f"{n_positional} positional argument(s), the documents give {len(self.params)}"
        known = set(self.params[self.posonly:]) | set(self.kwonly)
        unknown = [k for k in kws if k not in known]
        if unknown and not self.varkw:
            return False, f"keyword(s) {unknown} not in the documented signature ({', '.join(self.params + self.kwonly)})"
        if not star_args and not star_kw and self.defaults_known:
            covered = set(self.params[:n_positional]) | set(kws)
            missing = [p for p in self.params if p in self.required and p not in covered] + \
                      [k for k in self.kwonly if k in self.required and k not in covered]
            if missing:
                return False, f"required parameter(s) {missing} of the documented signature not given"
        return True, ""


@dataclass
class SignatureIndex:
    by_name: dict[str, list[Signature]] = field(default_factory=dict)     # "insert", "Table.insert", "IMAPClient"
    classes: set[str] = field(default_factory=set)

    def lookup(self, name: str) -> list[Signature]:
        """Signatures for a qualified or bare name; a bare method name matches any class's method of that name."""
        if name in self.by_name:
            return self.by_name[name]
        leaf = name.split(".")[-1]
        out = list(self.by_name.get(leaf, []))
        for k, v in self.by_name.items():
            if k.endswith("." + leaf):
                out += v
        return out

    def add(self, name: str, sig: Signature) -> None:
        lst = self.by_name.setdefault(name, [])
        if sig not in lst:
            lst.append(sig)


def _parse_params(args: str) -> Signature | None:
    text = args.strip()
    for attempt in (text, _strip_annotations(text)):
        try:
            fn = ast.parse(f"def f({attempt}): pass").body[0]
        except SyntaxError:
            continue
        a = fn.args
        pos = [x.arg for x in a.posonlyargs + a.args]
        drop = 1 if pos and pos[0] in ("self", "cls") else 0
        pos = pos[drop:]
        n_def = len(a.defaults)
        required = set(pos[: len(pos) - n_def]) if n_def else set(pos)
        kwonly = [x.arg for x in a.kwonlyargs]
        required |= {k for k, d in zip(kwonly, a.kw_defaults, strict=False) if d is None}
        return Signature(tuple(pos), frozenset(required), tuple(kwonly), a.vararg is not None, a.kwarg is not None,
                         max(0, len(a.posonlyargs) - drop), defaults_known=("=" in text or not pos))
    return None


def _strip_annotations(text: str) -> str:
    """'host: str, port: int = None, *args: Any' -> 'host, port=None, *args'."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        name, _, default = p.partition("=")
        name = name.split(":", 1)[0].strip()
        if not re.fullmatch(r"\*{0,2}[A-Za-z_]\w*|\*|/", name):
            return "*args, **kwargs"          # not a parameter list we can read: accept anything
        out.append(f"{name}=None" if default.strip() else name)
    return ", ".join(out)


_BASES = re.compile(r"^\s*(object|[A-Z]\w*(?:\.[A-Z]\w*)*)(\s*,\s*(object|[A-Z]\w*(?:\.[A-Z]\w*)*))*\s*$")


def _not_a_signature(name: str, args: str) -> bool:
    """`Range(object)` / `Invalid(Error)` name base classes; `items()` in prose says 'callable', not 'takes nothing'."""
    if not args.strip():
        return True
    leaf = name.split(".")[-1]
    return bool(leaf[:1].isupper() and _BASES.match(args))


def derive_signatures(docs: Mapping[str, str]) -> SignatureIndex:
    idx = SignatureIndex()
    for doc_name, text in docs.items():
        for m in _BACKTICK_CALL.finditer(text):
            if _not_a_signature(m.group("name"), m.group("args")):
                continue
            sig = _parse_params(m.group("args"))
            if sig is not None:
                idx.add(m.group("name"), Signature(**{**sig.__dict__, "source": doc_name}))
        current_class = ""
        for line in text.splitlines():
            c = _UML_CLASS.match(line)
            if c:
                current_class = c.group("name")
                idx.classes.add(current_class)
                continue
            if line.strip().startswith("}"):
                current_class = ""
                continue
            m = _UML_MEMBER.match(line)
            if m and current_class and not line.lstrip().startswith(("def ", "`", "-", "*")) \
                    and not _not_a_signature(m.group("name"), m.group("args")):
                sig = _parse_params(m.group("args"))
                if sig is not None:
                    name = m.group("name")
                    key = current_class if name == "__init__" else f"{current_class}.{name}"
                    idx.add(key, Signature(**{**sig.__dict__, "source": doc_name}))
        for m in _DEF.finditer(text):
            sig = _parse_params(m.group("args"))
            if sig is not None:
                idx.add(m.group("name"), Signature(**{**sig.__dict__, "source": doc_name}))
    # a class documented with __init__ under a qualified name is also callable by its own name
    for k in list(idx.by_name):
        if k.endswith(".__init__"):
            for s in idx.by_name[k]:
                idx.add(k.rsplit(".", 1)[0], s)
    return idx


__all__ = ["Signature", "SignatureIndex", "derive_signatures"]
