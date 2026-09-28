"""Render Python API and plugin API pages from the model and the manifest.

One page per symbol, in the order: import line, signature, explanation,
when to use, parameters (grouped per manifest `param_groups`), returns,
raises, members, examples, notes, "how is this different", related, guides.
Empty sections are left out. The page's summary is its frontmatter
`description`, which the site shows under the title.
"""

from __future__ import annotations

import re
from pathlib import Path

from tools.docs import config, docstrings, mdx
from tools.docs.manifest import Manifest, Page

GENERATED_NOTE = ("{/* GENERATED from %s. Edit the docstring or tools/docs/manifest.yaml, "
                  "then run: python tools/docs/sync_docs.py generate */}")


# -- linking -------------------------------------------------------------


class Linker:
    """Turn a code span that names a documented symbol into a link."""

    def __init__(self, manifest: Manifest, current: str | None = None):
        self.manifest = manifest
        self.current = current
        self.urls: dict[str, str] = {}
        for qualname, page in manifest.pages.items():
            self.urls[qualname] = page.url
        for alias, target in manifest.aliases.items():
            if target in manifest.pages:
                self.urls[alias] = manifest.pages[target].url
        short = {}
        for qualname, url in list(self.urls.items()):
            if qualname.startswith("plexora.api."):
                short[f"api.{qualname[len('plexora.api.'):]}"] = url
            elif qualname.count(".") == 1:
                name = qualname.split(".", 1)[1]
                if "_" in name or re.match(r"^[A-Z][a-z]+[A-Z]", name):
                    short[name] = url
        self.short = short

    def __call__(self, code: str) -> str | None:
        text = code.strip()
        text = re.sub(r"\(.*\)$", "", text)  # `create_project(...)` -> create_project
        url = self.urls.get(text) or self.short.get(text)
        if not url or (self.current and self.urls.get(self.current) == url):
            return None
        return url


# -- diagnostics ---------------------------------------------------------


def member_records(record: dict, manifest: Manifest) -> list[dict]:
    excluded = manifest.extras(record["qualname"]).get("exclude_members") or {}
    return [m for m in record.get("members") or [] if m["name"] not in excluded]


def field_records(record: dict, manifest: Manifest) -> list[dict]:
    excluded = manifest.extras(record["qualname"]).get("exclude_members") or {}
    return [f for f in record.get("fields") or [] if not f["hidden"] and f["name"] not in excluded]


def diagnose(record: dict, manifest: Manifest, symbols: dict) -> list[tuple[str, str]]:
    """Every docstring problem for one symbol, before waivers."""
    qualname = record["qualname"]
    allow_partial = qualname in {(i["symbol"] if isinstance(i, dict) else i)
                                 for i in manifest.docstrings.get("allow_partial") or []}
    out: list[tuple[str, str]] = []
    kind = record["kind"]
    doc = record.get("doc") or ""
    if kind == "constant":
        if not doc:
            out.append(("constant_without_doc_comment", ""))
        return out
    parsed = docstrings.parse(doc)
    if kind in ("class", "exception") and not doc:
        out.append(("class_without_docstring", ""))
    else:
        out.extend(parsed.diagnostics)
    for code, param in record.get("diagnostics") or []:
        out.append((code, param))
    known = set(manifest.pages) | set(manifest.aliases)
    for name in parsed.see_also:
        if name not in known and f"plexora.{name}" not in known:
            out.append(("see_also_unresolved", name))
    if kind == "function":
        out.extend(docstrings.check_signature(parsed, record.get("params"), record.get("returns"),
                                              allow_partial=allow_partial))
    if kind in ("class", "exception"):
        args_doc = parsed if parsed.args else docstrings.parse(record.get("init_doc") or "")
        if record.get("is_dataclass"):
            attrs = {e.name for e in parsed.attributes}
            for f in field_records(record, manifest):
                if not f["doc"] and f["name"] not in attrs:
                    out.append(("member_undocumented", f["name"]))
        elif record.get("params"):
            out.extend((c, p) for c, p in docstrings.check_signature(args_doc, record["params"], None)
                       if not (c == "param_undocumented" and p.lstrip("*") in ("args", "kwargs")
                               and kind == "exception"))
        for member in member_records(record, manifest):
            if member["kind"] == "attribute":
                if not member.get("doc"):
                    out.append(("member_undocumented", member["name"]))
                continue
            if not member.get("doc"):
                out.append(("member_undocumented", member["name"]))
                continue
            sub = docstrings.parse(member["doc"])
            out.extend((c, f"{member['name']}: {d}".rstrip(": ")) for c, d in sub.diagnostics)
            if member["kind"] != "property" and not member["name"].startswith("__"):
                out.extend((c, f"{member['name']}.{p}")
                           for c, p in docstrings.check_signature(sub, member.get("params"), member.get("returns")))
    if kind == "module":
        for name in manifest.module_members(qualname):
            sub = symbols.get(f"{qualname}.{name}")
            if sub and not sub.get("doc"):
                out.append(("member_undocumented", name))
    return out


def apply_waivers(qualname: str, found, manifest: Manifest):
    """`(remaining, stale_waivers)` after the manifest's waivers."""
    waivers = [w for w in manifest.docstrings.get("waivers") or [] if w["symbol"] == qualname]
    remaining = []
    used = set()
    for code, detail in found:
        match = next((i for i, w in enumerate(waivers)
                      if w["code"] == code and (not w.get("param") or w["param"] == detail)), None)
        if match is None:
            remaining.append((code, detail))
        else:
            used.add(match)
    stale = [w for i, w in enumerate(waivers) if i not in used]
    return remaining, stale


# -- rendering helpers ---------------------------------------------------


def import_line(qualname: str, record: dict) -> str:
    module, _, name = qualname.rpartition(".")
    if record["kind"] == "module":
        return f"from {module} import {name}"
    return f"from {module} import {name}"


def display_signature(record: dict, manifest: Manifest) -> str:
    """The signature as shown on the page: constructor parameters for
    members the manifest excludes are left out (the frontmatter keeps the
    full signature, which test_docs_signatures checks)."""
    from tools.docs.python_api import render_signature

    excluded = manifest.extras(record["qualname"]).get("exclude_members") or {}
    if not excluded or not record.get("params"):
        return record["signature"]
    params = [dict(p, hidden=p["hidden"] or p["name"] in excluded) for p in record["params"]]
    return render_signature(record["signature"].split("(", 1)[0], params, record.get("returns"))


def plain(text: str) -> str:
    """A summary for frontmatter: no Markdown markup."""
    text = re.sub(r"`([^`]*)`", r"\1", text or "")
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return " ".join(text.split())


def param_type(entry, param) -> str | None:
    if entry and entry.type:
        return entry.type
    return param.get("annotation") if param else None


def render_params(record: dict, parsed, manifest: Manifest, linker, heading="Parameters") -> str:
    excluded = manifest.extras(record["qualname"]).get("exclude_members") or {}
    params = [p for p in record.get("params") or []
              if not p["hidden"] and p["name"] not in ("self", "cls") and p["name"] not in excluded]
    if not params:
        return ""
    entries = {e.name.lstrip("*"): e for e in parsed.args}
    groups = manifest.extras(record["qualname"]).get("param_groups") or {}
    ordered: list[tuple[str | None, list[dict]]] = []
    placed = set()
    by_name = {p["name"]: p for p in params}
    for title, names in groups.items():
        members = [by_name[n] for n in names if n in by_name]
        placed.update(n for n in names)
        if members:
            ordered.append((title, members))
    rest = [p for p in params if p["name"] not in placed]
    if rest:
        ordered.append(("Other parameters" if groups else None, rest))
    tables = []
    for title, members in ordered:
        rows = []
        for p in members:
            entry = entries.get(p["name"])
            name = {"var-positional": "*", "var-keyword": "**"}.get(p["kind"], "") + p["name"]
            required = not p["has_default"] and p["kind"] not in ("var-positional", "var-keyword")
            kind = "keyword-only" if p["kind"] == "keyword-only" else None
            rows.append(mdx.component("Param", {
                "name": name,
                "type": param_type(entry, p),
                "default": p["default"] if p["has_default"] else None,
                "required": required,
                "kind": kind,
            }, mdx.escape(entry.description, linker) if entry else ""))
        tables.append(mdx.component("ParamTable", {"title": title}, "\n\n".join(rows)))
    return mdx.join(f"## {heading}", *tables)


def render_returns(record: dict, parsed, linker) -> str:
    entry = parsed.returns or parsed.yields
    annotation = record.get("returns")
    if not entry and not annotation:
        return ""
    if not entry and annotation in ("None",):
        return ""
    title = "Yields" if parsed.yields and not parsed.returns else "Returns"
    return mdx.join(f"## {title}", mdx.component("Returns", {"type": (entry.type if entry and entry.type else annotation)},
                                                  mdx.escape(entry.description, linker) if entry else ""))


def render_raises(parsed, linker) -> str:
    if not parsed.raises:
        return ""
    rows = [mdx.component("Raise", {"type": e.name}, mdx.escape(e.description, linker)) for e in parsed.raises]
    return mdx.join("## Raises", mdx.component("Raises", {}, "\n\n".join(rows)))


def prose_section(title: str, text: str, linker) -> str:
    return mdx.join(f"## {title}", mdx.escape(text, linker)) if text and text.strip() else ""


def guide_title(url: str) -> str:
    rel = url.removeprefix("/docs/").strip("/")
    for candidate in (config.CONTENT / f"{rel}.mdx", config.CONTENT / rel / "index.mdx"):
        if candidate.exists():
            match = re.search(r'^title:\s*"?(.*?)"?\s*$', candidate.read_text(encoding="utf-8"), re.M)
            if match:
                return match.group(1)
    return rel.rsplit("/", 1)[-1].replace("-", " ").capitalize()


def cards(items: list[tuple[str, str, str]]) -> str:
    rows = [mdx.component("Card", {"title": t, "href": h, "description": d or None}) for t, h, d in items]
    return mdx.component("Cards", {}, "\n".join(rows))


def title_of(qualname: str, symbols: dict) -> str:
    return qualname.rsplit(".", 1)[1]


def summary_of(qualname: str, symbols: dict) -> str:
    record = symbols.get(qualname) or {}
    return plain(docstrings.parse(record.get("doc") or "").summary)


def render_members(record: dict, manifest: Manifest, linker, symbols: dict) -> str:
    blocks = []
    qualname = record["qualname"]
    if record["kind"] == "module":
        for name in manifest.module_members(qualname):
            sub = symbols.get(f"{qualname}.{name}")
            if not sub:
                continue
            blocks.append(render_member_block(sub, manifest, linker))
        return mdx.join("## Members", *blocks) if blocks else ""
    members = member_records(record, manifest)
    properties = [m for m in members if m["kind"] in ("property", "attribute")]
    methods = [m for m in members if m["kind"] not in ("property", "attribute")]
    out = []
    if properties:
        out.append(mdx.join("## Properties", *(render_member_block(m, manifest, linker) for m in properties)))
    if methods:
        out.append(mdx.join("## Methods", *(render_member_block(m, manifest, linker) for m in methods)))
    return mdx.join(*out) if out else ""


def render_member_block(member: dict, manifest: Manifest, linker) -> str:
    parsed = docstrings.parse(member.get("doc") or "")
    name = member["name"]
    kind = member["kind"]
    parts = [f"### {name}" if not name.startswith("__") else f"### `{name}`"]
    if kind == "constant":
        parts.append(mdx.code_block(f"{name} = {member.get('value')}"))
    elif kind == "attribute":
        if member.get("value"):
            parts.append(mdx.code_block(f"{name} = {member['value']}"))
    elif kind == "property":
        annotation = member.get("annotation")
        parts.append(mdx.code_block(f"{name}" + (f": {annotation}" if annotation else "")))
    else:
        sig = member.get("signature") or name
        label = {"classmethod": "@classmethod\n", "staticmethod": "@staticmethod\n"}.get(kind, "")
        parts.append(mdx.code_block(label + mdx.wrap_signature(sig, 80)))
    if parsed.summary:
        parts.append(mdx.escape(parsed.summary, linker))
    if parsed.description:
        parts.append(mdx.escape(parsed.description, linker))
    if parsed.when_to_use:
        parts.append(f"**When to use.** {mdx.escape(parsed.when_to_use, linker)}")
    params = [p for p in member.get("params") or [] if not p["hidden"]]
    if params and parsed.args:
        entries = {e.name.lstrip("*"): e for e in parsed.args}
        rows = []
        for p in params:
            entry = entries.get(p["name"])
            rows.append(mdx.component("Param", {
                "name": {"var-positional": "*", "var-keyword": "**"}.get(p["kind"], "") + p["name"],
                "type": param_type(entry, p),
                "default": p["default"] if p["has_default"] else None,
                "required": not p["has_default"] and p["kind"] not in ("var-positional", "var-keyword"),
            }, mdx.escape(entry.description, linker) if entry else ""))
        parts.append(mdx.component("ParamTable", {}, "\n\n".join(rows)))
    entry = parsed.returns or parsed.yields
    if entry:
        parts.append(mdx.component("Returns", {"type": entry.type or member.get("returns")},
                                   mdx.escape(entry.description, linker)))
    if parsed.raises:
        parts.append(render_raises(parsed, linker).replace("## Raises\n\n", ""))
    if parsed.examples:
        parts.append(mdx.escape(parsed.examples, linker))
    for text in (parsed.notes, parsed.warnings):
        if text:
            parts.append(mdx.component("Callout", {}, mdx.escape(text, linker)))
    return mdx.join(*parts)


def render_fields(record: dict, parsed, manifest: Manifest, linker) -> str:
    fields = field_records(record, manifest)
    if not fields:
        return ""
    attrs = {e.name: e for e in parsed.attributes}
    rows = []
    for f in fields:
        entry = attrs.get(f["name"])
        text = entry.description if entry else f["doc"]
        rows.append(mdx.component("Param", {
            "name": f["name"],
            "type": (entry.type if entry and entry.type else f["annotation"]),
            "default": f["default"],
            "required": f["required"],
        }, mdx.escape(text, linker)))
    return mdx.join("## Fields", mdx.component("ParamTable", {}, "\n\n".join(rows)))


def aliases_of(qualname: str, manifest: Manifest, symbols: dict) -> list[str]:
    from tools.docs.manifest import auto_aliases

    out = [alias for alias, target in manifest.aliases.items() if target == qualname]
    out += [alias for alias, target in auto_aliases(symbols).items() if target == qualname]
    return sorted(set(out))


def search_terms(qualname: str, manifest: Manifest) -> list[str]:
    terms = list(manifest.extras(qualname).get("search") or [])
    for word, targets in (manifest.data.get("search_aliases") or {}).items():
        if qualname in targets and word not in terms:
            terms.append(word)
    return terms


# -- pages ---------------------------------------------------------------


def render_page(page: Page, record: dict, manifest: Manifest, symbols: dict) -> str:
    qualname = page.qualname
    linker = Linker(manifest, current=qualname)
    parsed = docstrings.parse(record.get("doc") or "")
    kind = record["kind"]
    extras = manifest.extras(qualname)
    title = title_of(qualname, symbols)
    badge = {"function": None, "class": "class", "exception": "exception", "constant": "constant",
             "module": "module"}.get(kind)

    front = mdx.frontmatter({
        "title": title,
        "description": plain(parsed.summary) or None,
        "generated": True,
        "symbol": qualname,
        "signature": record.get("signature"),
        "badge": badge,
        "aliases": search_terms(qualname, manifest) or None,
        "source": record.get("source"),
    })
    blocks = [front, GENERATED_NOTE % f"{record['module']}.{record['name']}" if kind != "module"
              else GENERATED_NOTE % record["module"]]

    head = [f"```python\n{import_line(qualname, record)}\n```"]
    also = aliases_of(qualname, manifest, symbols)
    if also:
        head.append("Also available as " + ", ".join(f"`{a}`" for a in also) + ".")
    if kind in ("function",) or (kind in ("class", "exception") and record.get("params") is not None):
        head.append(mdx.code_block(mdx.wrap_signature(display_signature(record, manifest))))
    if kind == "constant":
        head.append(mdx.code_block(f"{record['name']} = {record.get('value')}"))
    if kind == "exception" and record.get("bases"):
        head.append(f"Subclass of `{record['bases'][0]}`.")
    blocks.extend(head)

    if parsed.description:
        blocks.append(mdx.escape(parsed.description, linker))
    blocks.append(prose_section("When to use", parsed.when_to_use, linker))

    if kind == "function":
        blocks.append(render_params(record, parsed, manifest, linker))
        blocks.append(render_returns(record, parsed, linker))
    elif kind in ("class", "exception"):
        if record.get("is_dataclass"):
            blocks.append(render_fields(record, parsed, manifest, linker))
        else:
            init_parsed = parsed if parsed.args else docstrings.parse(record.get("init_doc") or "")
            if init_parsed is not parsed and init_parsed.summary:
                blocks.append(mdx.escape(init_parsed.summary + ("\n\n" + init_parsed.description
                                                               if init_parsed.description else ""), linker))
            blocks.append(render_params(record, init_parsed, manifest, linker, heading="Constructor parameters"))
            if parsed.attributes:
                rows = [mdx.component("Param", {"name": e.name, "type": e.type}, mdx.escape(e.description, linker))
                        for e in parsed.attributes]
                blocks.append(mdx.join("## Attributes", mdx.component("ParamTable", {}, "\n\n".join(rows))))
    blocks.append(render_raises(parsed, linker))
    blocks.append(render_members(record, manifest, linker, symbols))
    blocks.append(prose_section("Examples", parsed.examples, linker))
    notes = "\n\n".join(t for t in (parsed.notes, parsed.warnings) if t)
    blocks.append(prose_section("Notes", notes, linker))

    compares = [c for c in manifest.compare if qualname in c.get("symbols", [])]
    if compares:
        boxes = [mdx.component("Compare", {"title": c.get("title")}, mdx.escape(c["body"].strip(), linker)
                               if "|" not in c["body"] else c["body"].strip()) for c in compares]
        blocks.append(mdx.join("## How is this different?", *boxes))

    related = [r for r in extras.get("related") or [] if r in linker.urls]
    see_also = [n if n in linker.urls else f"plexora.{n}" for n in parsed.see_also]
    related += [n for n in see_also if n in linker.urls and n not in related]
    if related:
        items = []
        for r in related:
            target = manifest.aliases.get(r, r)
            items.append((title_of(r, symbols), linker.urls[r], summary_of(target, symbols)))
        blocks.append(mdx.join("## Related", cards(items)))
    guides = extras.get("guides") or []
    if guides:
        blocks.append(mdx.join("## Guides", cards([(guide_title(g), g, "") for g in guides])))
    return mdx.join(*blocks)


def render_category_index(section: dict, category: dict, manifest: Manifest, symbols: dict) -> str:
    items = []
    for qualname in category.get("symbols", []):
        page = manifest.pages[qualname]
        items.append((title_of(qualname, symbols), page.url, summary_of(qualname, symbols)))
    return mdx.join(
        mdx.frontmatter({"title": category["title"], "description": category.get("description"), "generated": True}),
        "{/* GENERATED from tools/docs/manifest.yaml. Run: python tools/docs/sync_docs.py generate */}",
        cards(items),
    )


def render_section_index(section: dict, manifest: Manifest) -> str:
    items = [(c["title"], f"/docs/{section['dir']}/{c['key']}", c.get("description", ""))
             for c in section.get("categories", [])]
    intro = {
        "python-api": ("Everything below is reachable as `plexora.<name>` after `import plexora`. "
                       "Each page is generated from the function's docstring and signature, so it always "
                       "matches the installed version. For task-oriented walkthroughs see the "
                       "[Python guides](/docs/python)."),
        "plugin-api/descriptor": ("A plugin package exposes a module-level `PLUGIN = Plugin(...)`. These are the "
                                  "names that descriptor is built from, all importable from `plexora.api`. See "
                                  "[Plugin development](/docs/plugin-development) for a walkthrough."),
        "plugin-api/runtime": ("What a plugin's Flask routes call on the server: read a project's image, mask and "
                               "cell table, keep state, and answer the browser. Everything here is importable from "
                               "`plexora.api`, the only module a plugin should import from Plexora."),
    }.get(section["dir"], "")
    return mdx.join(
        mdx.frontmatter({"title": section["title"], "description": section.get("description"), "generated": True}),
        "{/* GENERATED from tools/docs/manifest.yaml. Run: python tools/docs/sync_docs.py generate */}",
        intro,
        cards(items),
    )


def meta(title: str, pages: list[str], **extra) -> str:
    import json

    data = {"title": title, **extra, "pages": pages}
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def render_all(manifest: Manifest, symbols: dict) -> dict[str, str]:
    """`{path relative to content/docs: text}` for every Python-generated file."""
    out: dict[str, str] = {}
    for section in manifest.sections:
        base = section["dir"]
        out[f"{base}/index.mdx"] = render_section_index(section, manifest)
        extra = {"icon": section["icon"]} if section.get("icon") else {}
        out[f"{base}/meta.json"] = meta(section["title"], [*(c["key"] for c in section["categories"])],
                                        **extra)
        for category in section.get("categories", []):
            cdir = f"{base}/{category['key']}"
            slugs = []
            for qualname in category.get("symbols", []):
                page = manifest.pages[qualname]
                record = symbols[qualname]
                out[page.path] = render_page(page, record, manifest, symbols)
                slugs.append(page.slug)
            out[f"{cdir}/index.mdx"] = render_category_index(section, category, manifest, symbols)
            out[f"{cdir}/meta.json"] = meta(category["title"], [*slugs])
    return out
