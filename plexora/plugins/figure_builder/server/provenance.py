"""How a figure explains itself.

Two outputs from one function, because they are the same facts for two readers:
`manifest()` is the machine-readable record that ships beside the export, and
`lines()` is the page appended to a PDF for the human holding the printout.

The thing worth getting right is what counts as provenance. Not "this figure was
made in Plexora" -- that explains nothing. What a reader needs in order to
believe, or to reproduce, a panel:

* which image, and which REGION of it, in the image's own coordinates;
* how big that region is in microns, or an explicit statement that the scale is
  unknown;
* which channels, with the display window and colour each was drawn with;
* which plugin drew which overlay, and at what version;
* what the export could NOT reproduce, named rather than omitted.

That last one is the point of this file existing rather than the export just
writing a title page. An export that silently leaves out the phenotype colouring
a figure was made to show is an export that misrepresents the figure, and the
only defence against that is to say so in writing, on the artefact itself.
"""

from __future__ import annotations

from datetime import datetime, timezone

from plexora.plugins.figure_builder.server import compose


def manifest(document, results, options):
    """The machine-readable record: `figure-provenance.json`.

    Deliberately holds the scene snapshots verbatim. A manifest that summarised
    them would be a second description of the same thing, and the summary is the
    one that goes stale.
    """
    return {
        "manifest_version": 1,
        "generated_at": _now(),
        "figure": {
            "figure_id": document["figure_id"],
            "title": document["title"],
            "revision": document["revision"],
            "created_at": document["created_at"],
            "updated_at": document["updated_at"],
        },
        "export": {
            "format": options.get("format"),
            "dpi": options.get("dpi"),
            "pages": [page["page_id"] for page in document["pages"]],
        },
        "sources": [
            {
                "source_id": source["source_id"],
                "kind": source["kind"],
                "datasource": source["datasource"],
                "image": source["image"],
                "pixel_size": source["pixel_size"],
                "fingerprint": source["fingerprint"],
            }
            for source in document["sources"].values()
        ],
        "panels": [
            {
                "panel_id": panel["panel_id"],
                "label": result.get("label"),
                "source_id": panel["source_id"],
                "placement": panel["placement"],
                "scene": panel["scene"],
                "derived_from": panel["derived_from"],
                "render": result,
            }
            for panel, result in results
        ],
    }


def lines(document, results, options):
    """The provenance page, as lines of text.

    Text rather than a layout because it is appended as a plain page: a table
    that has to fit is a table that eventually does not, and a figure's
    provenance must never be the thing that fails to export.
    """
    out = [
        document["title"],
        "",
        f"Exported {_now()} · revision {document['revision']} · "
        f"{options.get('format', 'pdf').upper()} at {options.get('dpi')} DPI",
        "",
        "SOURCES",
    ]
    for source in document["sources"].values():
        name = source["display_name"] or source["datasource"] or source["source_id"]
        size = source["image"]
        scale = (f"{source['pixel_size']['value']:g} {source['pixel_size']['unit']}/px "
                 f"({source['pixel_size']['source']})"
                 if source["pixel_size"] else "scale information unavailable")
        out.append(f"  {name} — {size['width']} × {size['height']} px — {scale}")
    if not document["sources"]:
        out.append("  (none)")

    out += ["", "PANELS"]
    for panel, result in results:
        out.extend(_panel_lines(document, panel, result))

    limitations = _limitations(results)
    if limitations:
        out += ["", "NOT REPRODUCED IN THIS EXPORT"]
        out += [f"  {line}" for line in limitations]
        out += [
            "",
            "  These are stated rather than omitted. The panels above were",
            "  re-rendered from the source image at the resolution requested;",
            "  anything listed here was drawn by a tool whose data a figure does",
            "  not store, and appears only in the on-screen preview.",
        ]
    return out


def _panel_lines(document, panel, result):
    source = document["sources"].get(panel["source_id"], {})
    viewport = panel["scene"]["viewport"]
    label = result.get("label") or panel["panel_id"]
    name = source.get("display_name") or source.get("datasource") or "unknown source"

    out = [f"  {label} — {name}",
           f"      region  x {viewport['x']:.1f}  y {viewport['y']:.1f}  "
           f"w {viewport['w']:.1f}  h {viewport['h']:.1f}  (full-resolution image pixels)"]

    pixel_size = source.get("pixel_size")
    if pixel_size:
        across = viewport["w"] * pixel_size["value"]
        down = viewport["h"] * pixel_size["value"]
        out.append(f"      field   {compose.format_microns(across)} × "
                   f"{compose.format_microns(down)}")
    else:
        out.append("      field   scale information unavailable")

    if result.get("effective_dpi"):
        out.append(f"      source detail {result['effective_dpi']:g} DPI at this size"
                   + (f" (requested {result['requested_dpi']})"
                      if result.get("requested_dpi") else ""))

    for channel in panel["scene"]["channels"]:
        colour = channel["color"]
        out.append(
            f"      channel {channel['fullname_at_capture'] or channel['key']}"
            f"  window {channel['window'][0]:g}–{channel['window'][1]:g}"
            f"  rgb({colour['r']},{colour['g']},{colour['b']})")

    for name, contribution in panel["scene"]["plugins"].items():
        out.append(f"      overlay {name} {contribution['version']}")
    return out


def _limitations(results):
    channels = set()
    overlays = set()
    for _, result in results:
        channels.update(result.get("missing_channels") or [])
        overlays.update(result.get("missing_overlays") or [])
    out = []
    if channels:
        out.append("channels no longer present in the source: " + ", ".join(sorted(channels)))
    if overlays:
        out.append("overlays drawn by a tool rather than by the image: "
                   + ", ".join(sorted(overlays)))
    return out


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
