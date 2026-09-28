"""Parse Google-style docstrings (the SCIMAP convention) into sections.

    Summary sentence.

    Longer explanation, as Markdown. `code` in single backticks.

    When to use:
        Prose.

    Args:
        name (type, optional): What it is. Continuation lines are
            indented further.
        **answers: Keyword arguments are documented by their star form.

    Returns:
        type: What comes back.

    Raises:
        ValueError: When.

    Example:
        ```python
        import plexora
        ```

    Note:
        Prose.

    See Also:
        plexora.create_project, plexora.import_sample

The parser never fails: anything it cannot place becomes a diagnostic code
(see DIAGNOSTICS) that tests/test_docs_docstrings.py reports per symbol.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field

#: Section headers, lower-cased, and the canonical key each maps to.
SECTIONS = {
    "args": "args",
    "arguments": "args",
    "parameters": "args",
    "attributes": "attributes",
    "returns": "returns",
    "return": "returns",
    "yields": "yields",
    "raises": "raises",
    "example": "examples",
    "examples": "examples",
    "note": "notes",
    "notes": "notes",
    "warning": "warnings",
    "warnings": "warnings",
    "when to use": "when_to_use",
    "see also": "see_also",
}

#: Every diagnostic code, and what it means. The test prints these.
DIAGNOSTICS = {
    "no_summary": "The docstring is missing or has no summary sentence.",
    "class_without_docstring": "The class has no docstring of its own.",
    "member_undocumented": "A documented class member has no docstring.",
    "unknown_section": "A line looks like a section header but is not one Plexora uses.",
    "param_not_in_signature": "Args documents a parameter the signature does not have.",
    "param_undocumented": "A parameter in the signature is missing from Args.",
    "returns_missing": "The function is annotated to return a value but has no Returns section.",
    "empty_section": "A section header with nothing under it.",
    "see_also_unresolved": "See Also names something that is not a documented symbol.",
    "legacy_param_tag": "An @param/:param: tag; use an Args section.",
    "unfenced_example": "Example code that is not in a ``` fenced block.",
    "constant_without_doc_comment": "A public constant without a #: comment above it.",
    "unrepresentable_default": "A default value that has no readable text form.",
    "stale_waiver": "A waiver in manifest.yaml for a diagnostic that no longer occurs.",
}

_HEADER = re.compile(r"^([A-Za-z][A-Za-z ]*?):\s*$")
_LOOKS_LIKE_HEADER = re.compile(r"^[A-Z][a-z]+(?: [A-Za-z][a-z]+)?:\s*$")
_ENTRY = re.compile(r"^(\*{0,2}\w+)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$")
_RETURN_TYPE = re.compile(
    r"^((?:[\w.]+(?:\[[^\]]*(?:\[[^\]]*\][^\]]*)*\])?)(?:\s*\|\s*[\w.]+(?:\[[^\]]*(?:\[[^\]]*\][^\]]*)*\])?)*)\s*:\s*(.*)$")
_LEGACY = re.compile(r"(^|\s)(@param|@return|:param\s|:returns?:|:raises?\s)")


@dataclass
class Entry:
    name: str
    type: str | None
    optional: bool
    description: str


@dataclass
class Docstring:
    summary: str = ""
    description: str = ""
    args: list[Entry] = field(default_factory=list)
    attributes: list[Entry] = field(default_factory=list)
    returns: Entry | None = None
    yields: Entry | None = None
    raises: list[Entry] = field(default_factory=list)
    examples: str = ""
    notes: str = ""
    warnings: str = ""
    when_to_use: str = ""
    see_also: list[str] = field(default_factory=list)
    #: Codes found while parsing (signature-independent ones only).
    diagnostics: list[tuple[str, str]] = field(default_factory=list)
    present: set[str] = field(default_factory=set)


def _split_blocks(text: str):
    """Yield `(header or None, body lines)` with fenced code kept intact."""
    current_header = None
    body: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            body.append(line)
            continue
        if not in_fence and line == line.lstrip():
            match = _HEADER.match(line)
            if match and match.group(1).lower() in SECTIONS:
                yield current_header, body
                current_header, body = match.group(1), []
                continue
        body.append(line)
    yield current_header, body


def _dedent(lines: list[str]) -> str:
    return textwrap.dedent("\n".join(lines)).strip("\n")


def _entries(text: str, kind: str, diagnostics) -> list[Entry]:
    entries: list[Entry] = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line and not line[0].isspace():
            match = _ENTRY.match(line)
            if match:
                name, type_, desc = match.groups()
                optional = False
                if type_:
                    parts = [p.strip() for p in type_.split(",")]
                    if parts and parts[-1] == "optional":
                        optional = True
                        parts = parts[:-1]
                    type_ = ", ".join(parts) or None
                entries.append(Entry(name, type_, optional, desc.strip()))
                continue
            if kind == "raises":
                entries.append(Entry(line.strip().rstrip(":"), None, False, ""))
                continue
            diagnostics.append(("unknown_section", f"unparsed {kind} line: {line.strip()[:60]}"))
            continue
        if entries:
            entries[-1].description = (entries[-1].description + "\n" + line).strip("\n")
    for entry in entries:
        entry.description = textwrap.dedent(entry.description).strip() if "\n" in entry.description else entry.description
        if "\n" in entry.description:
            first, _, rest = entry.description.partition("\n")
            entry.description = first + "\n" + textwrap.dedent(rest)
    return entries


def _returns(text: str) -> Entry:
    first, _, rest = text.partition("\n")
    match = _RETURN_TYPE.match(first.strip())
    if match:
        desc = (match.group(2) + ("\n" + textwrap.dedent(rest) if rest else "")).strip()
        return Entry("", match.group(1), False, desc)
    return Entry("", None, False, text.strip())


def parse(doc: str) -> Docstring:
    result = Docstring()
    if not doc or not doc.strip():
        result.diagnostics.append(("no_summary", ""))
        return result
    doc = textwrap.dedent(doc).strip("\n")
    if _LEGACY.search(doc):
        result.diagnostics.append(("legacy_param_tag", ""))
    for header, lines in _split_blocks(doc):
        text = _dedent(lines)
        if header is None:
            summary, _, description = text.partition("\n\n")
            result.summary = " ".join(summary.split())
            result.description = description.strip()
            in_fence = False
            for line in description.splitlines():
                if line.strip().startswith("```"):
                    in_fence = not in_fence
                if not in_fence and _LOOKS_LIKE_HEADER.match(line) and line == line.lstrip():
                    result.diagnostics.append(("unknown_section", line.strip()))
            if not result.summary:
                result.diagnostics.append(("no_summary", ""))
            continue
        key = SECTIONS[header.lower()]
        result.present.add(key)
        if not text.strip():
            result.diagnostics.append(("empty_section", header))
            continue
        if key in ("args", "attributes", "raises"):
            setattr(result, key, _entries(text, key, result.diagnostics))
        elif key in ("returns", "yields"):
            setattr(result, key, _returns(text))
        elif key == "see_also":
            names = re.split(r"[,\n]", text)
            result.see_also = [n.strip().strip("`") for n in names if n.strip()]
        else:
            setattr(result, key, text)
            if key == "examples":
                _check_fenced(text, result.diagnostics)
    return result


def _check_fenced(text: str, diagnostics) -> None:
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and (line.startswith("    ") or line.lstrip().startswith(">>>")):
            diagnostics.append(("unfenced_example", line.strip()[:60]))
            return


def check_signature(parsed: Docstring, params, returns_annotation, *, allow_partial=False):
    """Diagnostics that need the signature: Args against parameters, and a
    Returns section for a function annotated to return something."""
    out: list[tuple[str, str]] = []
    if params is None:
        return out
    visible = [p for p in params if not p["hidden"] and p["name"] not in ("self", "cls")]
    def star(p):
        return {"var-positional": "*", "var-keyword": "**"}.get(p["kind"], "") + p["name"]
    documented = {e.name.lstrip("*") for e in parsed.args}
    names = {p["name"] for p in visible}
    for entry in parsed.args:
        if entry.name.lstrip("*") not in names:
            out.append(("param_not_in_signature", entry.name))
    if not allow_partial:
        for p in visible:
            if p["name"] not in documented:
                out.append(("param_undocumented", star(p)))
    if returns_annotation and returns_annotation not in ("None", "NoReturn") and parsed.returns is None \
            and parsed.yields is None:
        out.append(("returns_missing", returns_annotation))
    return out
