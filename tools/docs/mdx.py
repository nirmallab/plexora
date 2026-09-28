"""Turn docstring Markdown into MDX that cannot break the site build, and
emit the handful of components generated pages use.

MDX is stricter than Markdown: `{` starts a JavaScript expression, `<` a JSX
tag, and indented code is not code at all. A docstring written for `help()`
routinely contains all three (`{"image": ...}`, `<name>`, a four-space
example), and a single one fails `next build` with an error that names a
column in a generated file rather than the docstring. `escape()` is applied
to every piece of prose that leaves a docstring.
"""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"^(\s*)(```+|~~~+)")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_STAR_NAME = re.compile(r"(?<![\w*\\`])(\*{1,2})(?=[A-Za-z_]\w*(?![\w*]))")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def attr(value) -> str:
    """A JSX attribute value: always a JS expression holding a JSON literal,
    so quotes, braces and angle brackets in it need no further thought."""
    return "{" + json.dumps(value, ensure_ascii=False) + "}"


def _escape_text(text: str) -> str:
    text = _AUTOLINK.sub(lambda m: f"[{m.group(1)}]({m.group(1)})", text)
    text = text.replace("{", "\\{").replace("}", "\\}").replace("<", "\\<")
    def star(m):
        # `**answers` (Python varargs) is escaped; the opening of a bold or
        # italic span, which is closed later on the same line, is not.
        stars = m.group(1)
        closing = re.compile(r"[^\s*]" + re.escape(stars) + r"(?!\*)")
        if closing.search(text, m.end()):
            return stars
        return "\\*" * len(stars)
    text = _STAR_NAME.sub(star, text)
    return text


def _escape_line(line: str, linker=None) -> str:
    """Escape one prose line, leaving inline code spans untouched (and
    optionally turning code spans that name a documented symbol into links)."""
    out = []
    pos = 0
    for match in _INLINE_CODE.finditer(line):
        out.append(_escape_text(line[pos:match.start()]))
        code = match.group(0)
        href = linker(match.group(2)) if linker else None
        out.append(f"[{code}]({href})" if href else code)
        pos = match.end()
    out.append(_escape_text(line[pos:]))
    return "".join(out)


def _code_language(lines: list[str]) -> str:
    first = next((line.strip() for line in lines if line.strip()), "")
    if first.startswith("$ ") or re.match(r"^plexora(\s|$)", first) or first.startswith(("ssh ", "pip ", "srun ", "sbatch ")):
        return "bash"
    if first.startswith(("{", "[")) and not first.startswith("[p"):
        return "json"
    return "python"


def escape(markdown: str, linker=None) -> str:
    """Docstring Markdown -> MDX-safe Markdown.

    Fenced blocks pass through untouched. Blocks indented four or more
    spaces (after a blank line, not continuing a list item) become fenced
    blocks, since MDX has no indented code. Everything else is escaped line
    by line with inline code preserved.
    """
    if not markdown:
        return ""
    lines = markdown.splitlines()
    out: list[str] = []
    index = 0
    in_fence = None
    previous_blank = True
    in_list = False
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        if in_fence:
            out.append(line)
            if fence and fence.group(2)[0] == in_fence[0] and len(fence.group(2)) >= len(in_fence):
                in_fence = None
            index += 1
            continue
        if fence:
            in_fence = fence.group(2)
            out.append(line)
            index += 1
            continue
        if previous_blank and not in_list and line.startswith("    ") and line.strip():
            block = []
            while index < len(lines) and (lines[index].startswith("    ") or not lines[index].strip()):
                block.append(lines[index][4:] if lines[index].strip() else "")
                index += 1
            while block and not block[-1].strip():
                block.pop()
            out.append("```" + _code_language(block))
            out.extend(block)
            out.append("```")
            out.append("")
            previous_blank = True
            continue
        if _LIST_ITEM.match(line):
            in_list = True
        elif not line.strip():
            pass
        elif not line.startswith(" "):
            in_list = False
        out.append(_escape_line(line, linker))
        previous_blank = not line.strip()
        index += 1
    return "\n".join(out).strip("\n")


def inline(text: str, linker=None) -> str:
    """Escape a one-line string (a summary, a table cell)."""
    return _escape_line(" ".join((text or "").split()), linker)


def frontmatter(fields: dict) -> str:
    """YAML frontmatter with keys in the order given; every scalar JSON-encoded
    (valid YAML, and immune to colons and quotes in the value)."""
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == [] or value == {}:
            continue
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                if v is not None:
                    lines.append(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def code_block(code: str, language: str = "python") -> str:
    return f"```{language}\n{code.rstrip()}\n```"


def component(name: str, props: dict, children: str = "") -> str:
    """`<Name a={"..."}>children</Name>`, or self-closing without children.
    Props that are None/False are dropped; True renders as a bare flag."""
    parts = [name]
    for key, value in props.items():
        if value is None or value is False:
            continue
        parts.append(key if value is True else f"{key}={attr(value)}")
    head = " ".join(parts)
    if not children.strip():
        return f"<{head} />"
    return f"<{head}>\n\n{children.strip()}\n\n</{name}>"


def join(*blocks: str) -> str:
    """Join non-empty blocks with one blank line; a trailing newline."""
    return "\n\n".join(b.strip("\n") for b in blocks if b and b.strip()) + "\n"


def wrap_signature(signature: str, width: int = 88) -> str:
    """Break a long `name(a, b=1) -> T` into one parameter per line."""
    if len(signature) <= width:
        return signature
    head, _, rest = signature.partition("(")
    depth = 0
    params, current = [], ""
    for index, char in enumerate(rest):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                tail = rest[index + 1:]
                break
            depth -= 1
        if char == "," and depth == 0:
            params.append(current.strip())
            current = ""
            continue
        current += char
    else:
        tail = ""
    if current.strip():
        params.append(current.strip())
    body = "".join(f"    {p},\n" for p in params)
    return f"{head}(\n{body}){tail}"
