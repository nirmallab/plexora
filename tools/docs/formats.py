"""Supported file formats, rendered from the constants the importer uses.

The suffix lists are read from the code by the model dump (python_api.
`collect_formats`), so a format added to the importer appears here on the
next `generate`, and one removed disappears. Per-format notes come from
manifest.yaml `format_notes`; the folder formats (run outputs, stores) from
`format_folders`.
"""

from __future__ import annotations

from tools.docs import mdx


def render(manifest, formats: dict | None = None) -> dict[str, str]:
    formats = formats or manifest.data.get("_formats") or {}
    notes = manifest.data.get("format_notes") or {}
    image = formats.get("image_suffixes", [])
    brightfield = set(formats.get("brightfield_only", []))
    tables = formats.get("table_suffix_types", {})
    rows = ["| Extension | Read as | Notes |", "|---|---|---|"]
    for suffix in sorted(set(image) | brightfield):
        kind = "Brightfield whole-slide image" if suffix in brightfield else "Image"
        rows.append(f"| `{suffix}` | {kind} | {mdx.inline(notes.get(suffix, ''))} |")
    table_rows = ["| Extension | Read as | Notes |", "|---|---|---|"]
    names = {"csv": "Delimited table", "parquet": "Parquet table", "anndata": "AnnData"}
    for suffix, kind in sorted(tables.items()):
        table_rows.append(f"| `{suffix}` | {names.get(kind, kind)} | {mdx.inline(notes.get(suffix, ''))} |")
    folders = manifest.data.get("format_folders") or []
    folder_rows = ["| Folder or store | What Plexora reads | Guide |", "|---|---|---|"]
    for entry in folders:
        guide = f"[{entry['guide_title']}]({entry['guide']})" if entry.get("guide") else ""
        folder_rows.append(f"| {mdx.inline(entry['name'])} | {mdx.inline(entry['reads'])} | {guide} |")
    unsupported = manifest.data.get("format_unsupported") or []
    unsupported_rows = ["| Format | Status |", "|---|---|"]
    for entry in unsupported:
        unsupported_rows.append(f"| {mdx.inline(entry['name'])} | {mdx.inline(entry['status'])} |")
    kinds = ", ".join(f"`{k}`" for k in formats.get("resource_kinds", []))
    blocks = [
        mdx.frontmatter({
            "title": "Supported formats",
            "description": "Every image, table and folder format Plexora imports, read from the importer's own lists.",
            "generated": True,
            "aliases": ["file types", "extensions", "ome-tiff", "svs", "h5ad", "parquet"],
        }),
        "{/* GENERATED from the importer's suffix lists and tools/docs/manifest.yaml. "
        "Run: python tools/docs/sync_docs.py generate */}",
        mdx.escape("Plexora decides what a file is from its contents first and its extension second, so the "
                   "tables below list what each extension is tried as. A file that does not match what its "
                   "extension suggests is still identified correctly or refused with a message saying why."),
        mdx.join("## Images", "\n".join(rows)),
        mdx.join("## Cell tables", "\n".join(table_rows)),
    ]
    if folders:
        blocks.append(mdx.join("## Folders and run outputs", "\n".join(folder_rows)))
    if unsupported:
        blocks.append(mdx.join("## Not supported", mdx.escape(
            "These formats are recognised by name but not imported. See "
            "[Formats that are not supported yet](/docs/modalities/not-supported) for workarounds."),
            "\n".join(unsupported_rows)))
    if kinds:
        blocks.append(mdx.join("## Data node resources", mdx.escape(
            f"A data node serves resources of these kinds: {kinds}. See "
            "[Data nodes](/docs/remote/data-nodes).")))
    return {"reference/supported-formats.mdx": mdx.join(*blocks)}
