"""Structural VB6 indexing for this fork, without designer or binary resources.

This is a statement scanner, not a compiler. It records declarations and only
binds calls to unambiguous Sub/Function/Declare names in the same source file.
No raw_calls are exposed to the generic name-only cross-file resolver.
"""
from __future__ import annotations

import re
from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id


_NAME = r"[^\W\d]\w*"
_MODIFIERS = r"(?:(?:Public|Private|Friend|Static|Global)\s+)*"
_PROC = re.compile(
    rf"^{_MODIFIERS}(Sub|Function|Property\s+(?:Get|Let|Set))\s+({_NAME})\b(.*)",
    re.IGNORECASE,
)
_DECLARE = re.compile(
    rf"^{_MODIFIERS}Declare\s+(Sub|Function)\s+({_NAME})\b", re.IGNORECASE
)
_DATA = re.compile(
    rf"^{_MODIFIERS}(Type|Enum|Event|Const)\s+({_NAME})\b", re.IGNORECASE
)
_VARIABLE = re.compile(r"^(?:(?:Public|Private|Global|Dim|Static)\s+)+(.+)", re.IGNORECASE)
_PARAM = re.compile(rf"^\s*(?:(?:ByVal|ByRef|Optional|ParamArray|WithEvents)\s+)*({_NAME})", re.IGNORECASE)
_STRINGS = re.compile(r'"(?:[^"\n]|"")*"')


def _read_source(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Common Windows VB6 projects use the system ANSI code page. This fork
        # targets Western European/Portuguese projects; other code pages should
        # be converted to UTF-8 before indexing.
        return raw.decode("cp1252", errors="replace")


def _code_lines(text: str):
    """Drop VERSION/designer blocks, preserving original physical line numbers."""
    depth = 0
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if re.match(r"^(Begin|BeginProperty)\b", stripped, re.I):
            depth += 1
            continue
        if depth:
            if re.match(r"^(End|EndProperty)\s*$", stripped, re.I):
                depth -= 1
            continue
        if re.match(r"^(VERSION|Attribute)\b", stripped, re.I):
            continue
        yield line_no, line


def _statements(text: str):
    """Split outside strings/comments; join continuations with their first line."""
    pending = ""
    start = 1
    for line_no, line in _code_lines(text):
        if not pending:
            start = line_no
        parts = []
        buf = ""
        quoted = False
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"':
                if quoted and i + 1 < len(line) and line[i + 1] == '"':
                    buf += '""'
                    i += 2
                    continue
                quoted = not quoted
            if not quoted:
                rem_position = not buf.strip() or re.search(r"\b(?:Then|Else)\s+$", buf, re.I)
                if ch == "'" or (rem_position and re.match(r"Rem(?:\s|$)", line[i:], re.I)):
                    break
                if ch == ":" and not line[i:i + 2] == ":=":
                    parts.append(buf)
                    buf = ""
                    i += 1
                    continue
            buf += ch
            i += 1
        parts.append(buf)
        for index, part in enumerate(parts):
            combined = pending + part
            pending = ""
            if index == len(parts) - 1 and re.search(r"\s_\s*$", part) and not quoted:
                pending = re.sub(r"_\s*$", "", combined)
            elif combined.strip():
                yield start, combined.strip()
                start = line_no
    if pending.strip():
        yield start, pending.strip()


def _split_commas(text: str) -> list[str]:
    parts, start, depth = [], 0, 0
    masked = _STRINGS.sub(lambda m: " " * len(m[0]), text)
    for pos, char in enumerate(masked):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:pos])
            start = pos + 1
    return parts + [text[start:]]


class _Graph:
    def __init__(self, path: Path):
        self.path = path
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}

    def node(self, nid, label, kind, line, *, source=None, **metadata):
        self.nodes.setdefault(nid, {
            "id": nid, "label": label, "file_type": "code",
            "source_file": str(self.path) if source is None else source,
            "source_location": f"L{line}" if line else None,
            "metadata": {"language": "vb6", "kind": kind, **metadata},
        })
        return nid

    def edge(self, src, tgt, relation, line, **extra):
        self.edges.setdefault((src, tgt, relation, line), {
            "source": src, "target": tgt, "relation": relation,
            "confidence": "EXTRACTED", "source_file": str(self.path),
            "source_location": f"L{line}", "weight": 1.0, **extra,
        })

    def result(self):
        return {"nodes": list(self.nodes.values()), "edges": list(self.edges.values()),
                "input_tokens": 0, "output_tokens": 0}


def extract_vb6(path: Path) -> dict:
    """Index .bas/.cls/.frm executable declarations and conservative local calls."""
    try:
        text = _read_source(path)
    except (OSError, UnicodeError) as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}
    graph = _Graph(path)
    stem = _file_stem(path)
    file_id = graph.node(_make_id(str(path)), path.name, "file", 1)
    # Read attributes separately: their quoted values are not executable code.
    name_match = re.search(r'^\s*Attribute\s+VB_Name\s*=\s*"([^"]+)"', text, re.I | re.M)
    name = name_match[1] if name_match else path.stem
    kind = {".cls": "class", ".frm": "form"}.get(path.suffix.lower(), "module")
    module = graph.node(_make_id(stem, "vb6", name), name, kind, 1)
    graph.edge(file_id, module, "contains", 1)
    procedures = []
    targets: dict[str, list[str]] = {}
    module_variables: set[str] = set()
    current = None
    block = None

    for line, statement in _statements(text):
        masked = _STRINGS.sub(lambda m: " " * len(m[0]), statement)
        if statement.startswith("#"):
            # Index every conditional-compilation branch. Duplicate names have
            # separate nodes; the call pass will refuse ambiguous targets.
            continue
        if re.match(r"^End\s+(Sub|Function|Property)\b", masked, re.I):
            current = None
            continue
        if re.match(r"^End\s+(Type|Enum)\b", masked, re.I):
            block = None
            continue
        if block:
            member = re.match(rf"^({_NAME})\b", masked)
            if member:
                nid = graph.node(_make_id(block, member[1]), member[1], "field", line)
                graph.edge(block, nid, "contains", line)
            continue
        proc = _PROC.match(masked)
        decl = _DECLARE.match(masked)
        data = _DATA.match(masked)
        if proc or decl or data:
            match = proc or decl or data
            category = " ".join(match[1].lower().split())
            symbol = match[2]
            nid = _make_id(stem, "vb6", name, category, symbol)
            # Retain definitions in both #If branches instead of folding them.
            if nid in graph.nodes:
                nid = _make_id(nid, str(line))
            callable_ = bool(decl or (proc and category in {"sub", "function"}))
            label = f"{symbol}()" if callable_ else (
                f"{symbol} [{category.split()[-1].title()}]" if category.startswith("property ") else symbol
            )
            visibility = re.match(r"^(Public|Private|Friend|Global)\b", statement, re.I)
            owner = current["id"] if current and data else module
            if owner != module:
                nid = _make_id(owner, category, symbol)
            graph.node(nid, label, "declare" if decl else category, line,
                       name=symbol, visibility=visibility[1].lower() if visibility else "default")
            graph.edge(owner, nid, "method" if proc and kind != "module" else "contains", line)
            if callable_:
                graph.nodes[nid]["_callable"] = True
                targets.setdefault(symbol.casefold(), []).append(nid)
            if proc:
                # Everything declared locally shadows a module-level procedure.
                tail = proc[3].strip()
                params = tail[1:tail.rfind(")")] if tail.startswith("(") else ""
                shadow = {m[1].casefold() for p in _split_commas(params) if (m := _PARAM.match(p))}
                current = {"id": nid, "body": [], "shadow": shadow}
                procedures.append(current)
            elif category in {"type", "enum"}:
                block = nid
            elif current:
                current["shadow"].add(symbol.casefold())
            continue
        impl = re.match(rf"^Implements\s+({_NAME}(?:\.{_NAME})*)", masked, re.I)
        if impl and current is None:
            interface = graph.node(_make_id(stem, "vb6", "interface_ref", impl[1]),
                                   impl[1], "interface_reference", line, unresolved=True)
            graph.edge(module, interface, "implements", line)
            continue
        variables = _VARIABLE.match(masked)
        if variables:
            for part in _split_commas(variables[1]):
                var = _PARAM.match(part)
                if not var:
                    continue
                if current:
                    current["shadow"].add(var[1].casefold())
                else:
                    module_variables.add(var[1].casefold())
                    nid = graph.node(_make_id(stem, "vb6", name, "variable", var[1]),
                                     var[1], "variable", line)
                    graph.edge(module, nid, "contains", line)
            continue
        if current:
            current["body"].append((line, masked))

    for proc in procedures:
        for line, statement in proc["body"]:
            # Parenthesized expressions and statement-style calls, including
            # the Then/Else arms of a single-line If. Member calls deliberately
            # stay unresolved, including implicit receivers inside With blocks.
            candidates = {
                m[1] for m in re.finditer(rf"(?<![\w.!])({_NAME})[$%&!#@]?\s*\(", statement)
                if not statement[:m.start()].rstrip().endswith((".", "!"))
            }
            for arm in re.split(r"\b(?:Then|Else)\b", statement, flags=re.I):
                call = re.match(rf"^\s*(?:Call\s+)?({_NAME})\b(.*)", arm, re.I)
                if call and not re.match(r"\s*(?:=|\.|!)", call[2]):
                    candidates.add(call[1])
            for candidate in candidates:
                key = candidate.casefold()
                definitions = targets.get(key, [])
                if key not in proc["shadow"] | module_variables and len(definitions) == 1:
                    graph.edge(proc["id"], definitions[0], "calls", line, context="call")
    return graph.result()


def extract_vb6_project(path: Path) -> dict:
    """Index .vbp membership and external references without opening dependencies."""
    try:
        text = _read_source(path)
    except (OSError, UnicodeError) as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}
    graph = _Graph(path)
    project = graph.node(_make_id(str(path)), path.name, "project", 1)
    members = {"form", "module", "class", "usercontrol", "propertypage", "userdocument", "designer"}
    for line, statement in enumerate(text.splitlines(), 1):
        key, sep, value = statement.partition("=")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "name":
            graph.nodes[project]["metadata"]["name"] = value.strip('"')
        elif key in members:
            filename = value.split(";", 1)[-1].strip().strip('"')
            if not filename:
                continue
            # Normalize VB's Windows separators on every host. No referenced
            # file is read here; extraction stays restricted to scanned files.
            target = Path(filename.replace("\\", "/"))
            if not target.is_absolute():
                target = path.parent / target
            target = target.resolve()
            nid = graph.node(_make_id(str(target)), target.name, "file", 1,
                             source=str(target))
            graph.edge(project, nid, "contains", line, target_file=str(target))
        elif key in {"reference", "object"}:
            nid = graph.node(_make_id(_file_stem(path), "vb6", key, value),
                             value, "external_reference", line, external=True)
            graph.edge(project, nid, "references", line, context="import")
    return graph.result()
