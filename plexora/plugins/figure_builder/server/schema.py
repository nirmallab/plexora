"""What a figure IS, and what a valid piece of one looks like.

One versioned JSON document per figure holds the whole thing: the sources it
draws on, the pages, the panels and their captured scenes, the annotations, and
the revision the conflict check turns on.

    {
      "schema_version": 1,
      "figure_id": "fig_a1b2c3d4e5f6",
      "revision": 42,
      "title": "Figure 1",
      "created_at": "...", "updated_at": "...",
      "sources":     {"src_1": {...}},
      "pages":       [{"page_id": "pg_1", ...}],
      "panels":      {"pnl_1": {...}},
      "annotations": {"ann_1": {...}},
      "link_groups": {"grp_1": {...}},
      "groups":      {"grp_2": {...}},
      "settings":    {"dpi_default": 300, "style": {...}}
    }

Four structural decisions worth stating, because all four are cheap now and
expensive later:

**No image data, ever.** A source is a reference plus a fingerprint. The heavy
thing a figure points at -- a 90,000 x 76,000 pyramid -- stays where it is, and
a figure stays a few hundred kilobytes however many panels it holds.

**A page does not list its panels.** Membership is `panel.placement.page_id` and
nothing else. A page carrying `panel_ids` as well would be a second answer to
the same question, and the two answers drift the first time an operation
forgets one of them -- producing a panel that is on a page and not on it. Order
within a page is `placement.z`. A panel with `placement: null` is in the tray:
captured, kept, not laid out.

**Geometry is in two different units on purpose.** Everything about the SOURCE
-- `scene.viewport` -- is full-resolution image pixels. Everything about the
PAGE -- placement, annotations, page size -- is millimetres. Mixing them is how
a figure ends up rendering at the resolution of the screen it was built on. The
two never meet until export, where mm x DPI decides how many source pixels to
read.

**Physical scale is recorded, never inferred.** `source.pixel_size` is what the
OME metadata said at capture time, or null. A null disables scale bars and says
so; nothing anywhere multiplies a guess.
"""

from __future__ import annotations

import re

from plexora.plugins.figure_builder.server import textmetrics

SCHEMA_VERSION = 1

#: The snapshot inside a panel is versioned separately from the document that
#: holds it. They change for different reasons -- a new page property is not a
#: new way of describing a viewer -- and a figure written before a snapshot
#: field existed must still open.
SNAPSHOT_VERSION = 1


class UnreadableFigure(Exception):
    """This figure cannot be read by this build.

    Raised rather than shrugged off. Reading a newer document with today's rules
    means quietly dropping whatever the newer schema added, and then writing
    that loss back on the next autosave -- which turns "this build is too old"
    into "your panels are gone", with nothing on screen that says so.
    """


#: Ids are generated server-side for figures and client-side for everything
#: inside one (so a panel appears the instant it is captured rather than after
#: a round trip). Validated rather than trusted: they end up in DOM attributes,
#: selectors and file paths.
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

#: Figure ids additionally become a DIRECTORY NAME under data_path/.figures/,
#: so they are held to a much narrower rule than ids that only live inside the
#: document. No dots, no colons, no dashes -- nothing that could be a path, a
#: drive letter or an extension on any platform.
FIGURE_ID_PATTERN = re.compile(r"^fig_[a-z0-9]{6,32}$")

MAX_TITLE_LENGTH = 200
MAX_TEXT_LENGTH = 4_000
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Control characters that may not appear INSIDE a run. A run is one span of
#: uniformly styled text on one line, so a newline in it would be a line break
#: the line list does not know about -- `rich.lines` is the only place a break
#: is recorded, and two answers is one too many.
_RUN_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: Ceilings on one text annotation's structure. The character budget is still
#: MAX_TEXT_LENGTH and these sit under it: a paste can only be so long, but it
#: could still arrive as four thousand one-character runs from a word processor
#: that marks up every letter. Coalescing removes most of that; these stop the
#: rest.
MAX_TEXT_LINES = 200
MAX_RUNS_PER_LINE = 100
MAX_TEXT_RUNS = 500

COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")

#: Ceilings on one figure. Generous -- these exist to stop a runaway client, not
#: to tell anyone how many panels their figure has.
MAX_PAGES = 200
MAX_PANELS = 2_000
MAX_ANNOTATIONS = 5_000
MAX_SOURCES = 200
MAX_GROUPS = 1_000

#: Ceiling on one shape's node list. A freehand stroke is simplified on the
#: client long before it is sent, so this is not the working limit -- it is what
#: stops a client that skipped that step from writing a hundred thousand pointer
#: samples into a document that then has to be loaded again.
MAX_SHAPE_NODES = 500

#: How far outside its own box a shape's coordinates may reach. Handles sit past
#: their anchor as a matter of course and a node dragged outside the box is
#: normal until the client renormalises, so this is slack, not a geometry rule.
SHAPE_COORD_SLACK = 4.0

#: The shapes the picker offers. `preset` is a LABEL: the nodes are the geometry
#: for every one of them, "rect" included. It survives so the icon grid and the
#: creation defaults can name what a shape started as, and entering Edit Points
#: rewrites it to "custom" -- see `normalize_shape`.
SHAPE_PRESETS = (
    "rect", "rounded_rect", "ellipse", "capsule",
    "triangle", "right_triangle", "pentagon", "hexagon", "octagon",
    "diamond", "trapezoid", "parallelogram",
    "star5", "star6", "burst",
    "bar", "pill",
    "custom",
)

#: Page sizes in millimetres. Journal column widths are deliberately absent:
#: they change, they differ per publisher, and a hard-coded list that is wrong
#: is worse than no list. `custom` is any w/h the user types.
PAGE_PRESETS = {
    "a4": (210.0, 297.0),
    "letter": (215.9, 279.4),
    "square": (200.0, 200.0),
}
DEFAULT_PRESET = "a4"

#: Smallest and largest page a figure may declare, in mm. The lower bound stops
#: a divide-by-a-hair when panel geometry is scaled to the page; the upper one
#: stops a typo asking the export renderer for a 40-metre canvas.
MIN_PAGE_MM = 10.0
MAX_PAGE_MM = 2_000.0

#: `rect` and `ellipse` are no longer creatable -- the picker arms `shape`,
#: whose node list describes both of them and everything else besides. They
#: stay in the tuple because every figure drawn before the shape tool existed
#: is full of them, and removing a type here deletes every annotation of that
#: type on the next read (see `normalize_document`).
ANNOTATION_TYPES = ("text", "arrow", "line", "rect", "ellipse", "shape")

#: How a line's shaft is drawn. The dash arrays themselves are NOT stored --
#: `strokegeom.dash_pattern` derives them from this name and the pen width, so a
#: document can never hand the PDF writer a pattern it refuses.
LINE_STYLES = ("solid", "dashed", "dotted")

#: What may sit at either end of a line. "open" is the two barbs every arrow
#: drawn before this existed had; the rest are new. An arrow is a line whose
#: `end_head` is not "none" -- there is no separate arrow geometry anywhere in
#: this tree any more.
HEAD_STYLES = ("none", "open", "filled", "bar", "diamond")

#: One control, seven values, because tapering and fading are alternatives
#: rather than things to combine: a shaft is either a constant-width stroke, a
#: filled ribbon that narrows, or a stroke whose opacity ramps. Combining a
#: taper with a fade would need a gradient-filled polygon, which is a fourth
#: renderer path for a look nobody asked for.
LINE_EDGES = ("standard",
              "taper_start", "taper_end", "taper_both",
              "fade_start", "fade_end", "fade_both")

#: The longest head a document may ask for, in points -- an inch. Above that the
#: head is the annotation and the line is a detail of it, and the number is far
#: more likely to be a unit mix-up than an intention.
MAX_HEAD_SIZE_PT = 72.0


#: Where a piece of furniture sits inside its panel.
#:
#: Nine anchors and not free coordinates, deliberately. A scale bar dragged to
#: an arbitrary spot in one panel and a slightly different spot in the next is
#: the commonest way a figure looks hand-made; naming the corner means a row of
#: six panels agrees by construction. The names are read by both renderers
#: through `compose.anchor_box`, so the canvas and the export cannot disagree.
PANEL_ANCHORS = ("top_left", "top_center", "top_right",
                 "middle_left", "center", "middle_right",
                 "bottom_left", "bottom_center", "bottom_right")

#: How a scale-bar length is written. "auto" is the historical behaviour --
#: microns below a millimetre, millimetres above -- and stays the default so
#: that no figure made before this existed changes when it is reopened. The
#: rest force one unit, which is what makes a row of panels comparable at a
#: glance instead of one saying "500 µm" and its neighbour "1 mm".
SCALEBAR_UNITS = ("auto", "nm", "um", "mm")

#: Which way a colour bar runs. Separate from its anchor because the two are
#: genuinely independent: a vertical bar in the bottom-left corner is an
#: ordinary thing to want, and folding orientation into the nine anchors would
#: make eighteen names for what is two decisions.
COLORBAR_ORIENTATIONS = ("horizontal", "vertical")

#: How many colour stops a rendered colour bar is drawn with. Enough that the
#: ramp reads as continuous at print size, few enough that a page of six panels
#: with three channels each is not ten thousand rectangles in the PDF.
COLORBAR_STOPS = 48

#: The most ticks a colour bar may carry, and the most free labels a panel may.
#: Both are limits on how much furniture one panel can be given, not on what is
#: sensible: a colour bar with nine ticks is already a plot axis, and a panel
#: with twenty-four captions is a text box that should be an annotation.
MAX_COLORBAR_TICKS = 9
MAX_PANEL_LABELS = 24


#: A page's background is a hex colour or this. See page_background().
TRANSPARENT = "transparent"

SOURCE_KINDS = ("plexora_project", "imported_asset")

#: How a source compares against the project it names, recomputed on open.
#: `changed` never rerenders anything on its own -- see repository.py.
SOURCE_STATUSES = ("ok", "changed", "missing", "unknown")

DEFAULT_TITLE = "Untitled Figure"


# -- documents ----------------------------------------------------------


def new_document(figure_id, title=None, created_at=None):
    """A figure nobody has put anything in yet.

    One empty A4 page, because a figure with no pages has nowhere to drop the
    first captured panel and "add a page" is not a decision anyone wants to make
    before they have seen a panel.
    """
    stamp = created_at or ""
    return {
        "schema_version": SCHEMA_VERSION,
        "figure_id": figure_id,
        "revision": 0,
        "title": clean_text(title) or DEFAULT_TITLE,
        "created_at": stamp,
        "updated_at": stamp,
        "sources": {},
        "pages": [new_page("pg_1", name="Page 1")],
        "panels": {},
        "annotations": {},
        "link_groups": {},
        "groups": {},
        "settings": default_settings(),
    }


def normalize_document(raw, figure_id=None):
    """Coerce a loaded document into the current shape.

    Tolerant on the way in and strict on the way out, like ROI's: an older
    document is upgraded, and an entry that cannot be understood is dropped
    rather than allowed to half-load. Losing one malformed panel is better than
    refusing to open a figure that represents a day of work -- but a document
    from the FUTURE is refused outright, because there is no safe way to guess
    what was dropped.
    """
    if not isinstance(raw, dict):
        raise UnreadableFigure("this figure's document is not an object")

    version = raw.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
        raise UnreadableFigure(
            f"this figure was written by a newer version of Plexora "
            f"(schema {version}, this build reads {SCHEMA_VERSION})"
        )

    sources = {}
    for key, entry in (raw.get("sources") or {}).items():
        try:
            source = normalize_source({**entry, "source_id": key} if isinstance(entry, dict) else {})
        except ValueError:
            continue
        sources[source["source_id"]] = source

    pages = []
    seen_pages = set()
    for entry in raw.get("pages") or []:
        if not isinstance(entry, dict):
            continue
        try:
            page = normalize_page(entry)
        except ValueError:
            continue
        if page["page_id"] in seen_pages:
            continue
        seen_pages.add(page["page_id"])
        pages.append(page)
    if not pages:
        pages = [new_page("pg_1", name="Page 1")]
        seen_pages = {"pg_1"}

    panels = {}
    for key, entry in (raw.get("panels") or {}).items():
        try:
            panel = normalize_panel({**entry, "panel_id": key} if isinstance(entry, dict) else {})
        except ValueError:
            continue
        # A panel whose page was dropped is not deleted -- it goes back to the
        # tray, where the user can see it and decide. Silently discarding a
        # captured scene because its page failed to parse is the one outcome
        # nobody wants.
        if panel["placement"] and panel["placement"]["page_id"] not in seen_pages:
            panel["placement"] = None
        panels[panel["panel_id"]] = panel

    annotations = {}
    for key, entry in (raw.get("annotations") or {}).items():
        try:
            annotation = normalize_annotation(
                {**entry, "annotation_id": key} if isinstance(entry, dict) else {})
        except ValueError:
            continue
        if annotation["page_id"] not in seen_pages:
            continue
        annotations[annotation["annotation_id"]] = annotation

    groups = {}
    for key, entry in (raw.get("link_groups") or {}).items():
        try:
            group = normalize_link_group(
                {**entry, "group_id": key} if isinstance(entry, dict) else {})
        except ValueError:
            continue
        group["panel_ids"] = [p for p in group["panel_ids"] if p in panels]
        if len(group["panel_ids"]) < 2:
            continue
        groups[group["group_id"]] = group

    # A panel's own link_group is advisory; the group's membership list is the
    # rule. Reconciled here so nothing downstream has to handle a panel that
    # names a group the group does not name back.
    membership = {panel_id: gid for gid, g in groups.items() for panel_id in g["panel_ids"]}
    for panel_id, panel in panels.items():
        panel["link_group"] = membership.get(panel_id)

    # Visual groups. A document written before this key existed simply has
    # none, which is why the field is additive rather than a schema bump: an
    # older Plexora reading a figure with groups loses the grouping and nothing
    # else, and that is a legible outcome rather than a corrupted one.
    #
    # Membership lives ONLY here -- unlike link_groups, a member carries no
    # back-reference. There is nowhere to put one on an annotation without
    # adding a field to two shapes, and one answer to "what is grouped" cannot
    # drift from itself.
    visual_groups = {}
    for key, entry in (raw.get("groups") or {}).items():
        try:
            group = normalize_group(
                {**entry, "group_id": key} if isinstance(entry, dict) else {})
        except ValueError:
            continue
        group["member_ids"] = [m for m in group["member_ids"]
                               if m in panels or m in annotations]
        # A group of one selects one thing, which is what a click already does.
        if len(group["member_ids"]) < 2:
            continue
        visual_groups[group["group_id"]] = group

    # Nothing belongs to two groups. The first one wins rather than the last,
    # so a reload is deterministic.
    claimed = set()
    for group_id in list(visual_groups):
        members = [m for m in visual_groups[group_id]["member_ids"] if m not in claimed]
        if len(members) < 2:
            visual_groups.pop(group_id)
            continue
        visual_groups[group_id]["member_ids"] = members
        claimed.update(members)

    revision = raw.get("revision")
    return {
        "schema_version": SCHEMA_VERSION,
        "figure_id": clean_id(raw.get("figure_id")) or figure_id or "",
        "revision": 0 if isinstance(revision, bool) else max(0, as_int(revision, 0)),
        "title": clean_text(raw.get("title")) or DEFAULT_TITLE,
        "created_at": clean_text(raw.get("created_at")),
        "updated_at": clean_text(raw.get("updated_at")),
        "sources": sources,
        "pages": pages,
        "panels": panels,
        "annotations": annotations,
        "link_groups": groups,
        "groups": visual_groups,
        "settings": normalize_settings(raw.get("settings")),
    }


def default_settings():
    """Document-level defaults every object inherits and any object may override.

    `dpi_default` is what the export dialog opens on, not a promise: whether the
    source can actually supply that many pixels is checked at export time
    against the pyramid, and reported rather than silently downscaled.
    """
    return {
        "dpi_default": 300,
        "style": {
            "font_family": "Helvetica",
            "font_size_pt": 8.0,
            "label_size_pt": 10.0,
            "title_size_pt": 9.0,
            "gutter_mm": 3.0,
            "line_width_pt": 0.75,
            "text_color": "#000000",
            "panel_background": "#000000",
        },
        "label_style": "A",
    }


def normalize_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    defaults = default_settings()
    style = {**defaults["style"]}
    for key, value in (raw.get("style") or {}).items():
        if key not in style:
            continue
        if key in ("text_color", "panel_background"):
            style[key] = color(value, style[key])
        elif key == "font_family":
            style[key] = clean_text(value) or style[key]
        else:
            style[key] = as_float(value, style[key])
    label_style = clean_text(raw.get("label_style")) or defaults["label_style"]
    return {
        "dpi_default": clamp(as_int(raw.get("dpi_default"), defaults["dpi_default"]), 72, 1200),
        "style": style,
        "label_style": label_style if label_style in ("A", "a", "A1") else defaults["label_style"],
    }


# -- pages --------------------------------------------------------------


def new_page(page_id, name="", preset=DEFAULT_PRESET, orientation="portrait"):
    width, height = PAGE_PRESETS.get(preset, PAGE_PRESETS[DEFAULT_PRESET])
    if orientation == "landscape":
        width, height = height, width
    return {
        "page_id": page_id,
        "name": clean_text(name) or page_id,
        "preset": preset if preset in PAGE_PRESETS else "custom",
        "orientation": orientation if orientation in ("portrait", "landscape") else "portrait",
        "size_mm": {"w": width, "h": height},
        "margins_mm": {"top": 10.0, "right": 10.0, "bottom": 10.0, "left": 10.0},
        "background": "#ffffff",
    }


def normalize_page(raw):
    size = raw.get("size_mm") if isinstance(raw.get("size_mm"), dict) else {}
    margins = raw.get("margins_mm") if isinstance(raw.get("margins_mm"), dict) else {}
    preset = clean_text(raw.get("preset")) or "custom"
    orientation = clean_text(raw.get("orientation")) or "portrait"
    fallback = new_page(validate_id(raw.get("page_id"), "page id"),
                        preset=preset, orientation=orientation)
    return {
        "page_id": fallback["page_id"],
        "name": clean_text(raw.get("name")) or fallback["page_id"],
        "preset": preset if preset in PAGE_PRESETS else "custom",
        "orientation": fallback["orientation"],
        "size_mm": {
            "w": clamp(as_float(size.get("w"), fallback["size_mm"]["w"]), MIN_PAGE_MM, MAX_PAGE_MM),
            "h": clamp(as_float(size.get("h"), fallback["size_mm"]["h"]), MIN_PAGE_MM, MAX_PAGE_MM),
        },
        "margins_mm": {
            side: clamp(as_float(margins.get(side), 10.0), 0.0, MAX_PAGE_MM / 2)
            for side in ("top", "right", "bottom", "left")
        },
        "background": page_background(raw.get("background")),
    }


def page_background(value):
    """A hex colour, or the literal "transparent".

    Transparency is a page property rather than a colour because that is what
    it is: a figure destined for a dark-background slide, or for a journal that
    composites it onto its own paper, has no background rather than a white one.
    Sentinel string rather than a null, so `page["background"]` is always a
    string and every reader keeps one type.

    Only PNG can honour it. PDF and TIFF are told to render white and the
    export dialog says so -- silently flattening onto black, which is what an
    unhandled alpha channel usually produces, would ruin a figure at the last
    step.
    """
    if isinstance(value, str) and value.strip().lower() == TRANSPARENT:
        return TRANSPARENT
    return color(value, "#ffffff")

# -- sources ------------------------------------------------------------


def normalize_source(raw):
    """One image a figure draws on, as a reference and a fingerprint.

    `datasource` is the config.json key, which is the only stable image identity
    Plexora has -- there is no separate image asset id to fall back on. The
    fingerprint is what `changed` is decided by later: dimensions, the channel
    keys, and whether a mask was there. Hashing the pixels was considered and
    rejected; a fingerprint that takes four minutes to compute is one nobody
    computes.
    """
    kind = clean_text(raw.get("kind")) or "plexora_project"
    if kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source kind {kind!r}")

    image = raw.get("image") if isinstance(raw.get("image"), dict) else {}
    pixel_size = normalize_pixel_size(raw.get("pixel_size"))

    status = clean_text(raw.get("status")) or "unknown"
    return {
        "source_id": validate_id(raw.get("source_id"), "source id"),
        "kind": kind,
        "datasource": clean_text(raw.get("datasource")),
        "asset_id": clean_text(raw.get("asset_id")),
        "display_name": clean_text(raw.get("display_name")),
        "image": {
            "width": as_int(image.get("width"), 0),
            "height": as_int(image.get("height"), 0),
        },
        "pixel_size": pixel_size,
        "channels": [
            {"key": clean_text(c.get("key")),
             "fullname_at_capture": clean_text(c.get("fullname_at_capture"))}
            for c in raw.get("channels") or []
            if isinstance(c, dict) and clean_text(c.get("key"))
        ],
        "fingerprint": normalize_fingerprint(raw.get("fingerprint")),
        "status": status if status in SOURCE_STATUSES else "unknown",
    }


def normalize_pixel_size(raw):
    """Microns per pixel, or None -- never a default.

    A missing calibration disables scale bars and says so on the panel. The
    alternative, quietly assuming some conventional value, produces a figure
    with a scale bar that is simply wrong and looks exactly like one that is
    right.
    """
    if not isinstance(raw, dict):
        return None
    value = as_float(raw.get("value"), 0.0)
    if value <= 0:
        return None
    return {
        "value": value,
        "unit": clean_text(raw.get("unit")) or "µm",
        # Recorded because it changes what the number means: a value the user
        # typed for an uncalibrated import is not the same evidence as one the
        # file stated, and the provenance page says which.
        "source": "manual" if clean_text(raw.get("source")) == "manual" else "metadata",
    }


def normalize_fingerprint(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        "image_width": as_int(raw.get("image_width"), 0),
        "image_height": as_int(raw.get("image_height"), 0),
        "channel_keys": [clean_text(k) for k in raw.get("channel_keys") or [] if clean_text(k)],
        "has_segmentation": bool(raw.get("has_segmentation", False)),
    }


# -- panels -------------------------------------------------------------


def normalize_panel(raw):
    placement = raw.get("placement")
    label = raw.get("label") if isinstance(raw.get("label"), dict) else {}
    scalebar = raw.get("scalebar") if isinstance(raw.get("scalebar"), dict) else {}
    legend = raw.get("legend") if isinstance(raw.get("legend"), dict) else {}
    derived = raw.get("derived_from") if isinstance(raw.get("derived_from"), dict) else None

    return {
        "panel_id": validate_id(raw.get("panel_id"), "panel id"),
        "source_id": validate_id(raw.get("source_id"), "source id"),
        "scene": normalize_scene(raw.get("scene")),
        "placement": normalize_placement(placement) if isinstance(placement, dict) else None,
        "label": {
            "text": clean_text(label.get("text"), 8),
            # Auto labels renumber when panels are rearranged; a label the user
            # typed never does. One flag, so renumbering does not have to guess
            # which of "A" and "A'" it is allowed to overwrite.
            "auto": bool(label.get("auto", True)),
            "visible": bool(label.get("visible", True)),
        },
        "title": clean_text(raw.get("title")),
        "scalebar": normalize_scalebar(scalebar),
        "colorbar": normalize_colorbar(
            raw.get("colorbar") if isinstance(raw.get("colorbar"), dict) else {}),
        # Free captions on the image, as many as the panel needs. Distinct from
        # `label`, which is the figure's own A/B/C and is one per panel by
        # definition, and from a text annotation, which sits on the PAGE and
        # does not travel when the panel is moved.
        "labels": [normalize_panel_label(item) for item in raw.get("labels") or []
                   if isinstance(item, dict) and clean_id(item.get("label_id"))
                   ][:MAX_PANEL_LABELS],
        "legend": {
            # Channels and nothing else. A legend used to be able to list what
            # the OVERLAY plugins were drawing as well, which meant a figure's
            # legend depended on which plugins happened to be installed when it
            # was captured -- and a row for a phenotype the exported raster does
            # not draw (export renders channels) is a legend that lies about the
            # panel above it. Overlay rows are dropped on read; the flag that
            # asked for them is gone.
            "channels": bool(legend.get("channels", False)),
        },
        "link_group": clean_id(raw.get("link_group")) or None,
        "render_revision": max(0, as_int(raw.get("render_revision"), 0)),
        "derived_from": {
            "panel_id": clean_id(derived.get("panel_id")),
            "operation": clean_text(derived.get("operation"), 40),
            "layer": clean_text(derived.get("layer")),
        } if derived else None,
        "created_at": clean_text(raw.get("created_at")),
        "updated_at": clean_text(raw.get("updated_at")),
    }


def normalize_scalebar(raw):
    """A scale bar and everything about how it is drawn.

    Every appearance field defaults to what the bar looked like before any of
    them existed -- white, bottom right, 0.8 mm thick, 1.2 mm in, labelled at
    the figure's own font size -- so reopening a figure made before this does
    not move or restyle its bars. That is why the two size fields default to
    None rather than to a number: None means "the figure's", and a figure whose
    body text is later changed from 8 pt to 7 pt takes its scale-bar captions
    with it, which is what a user changing the figure's font expects.
    """
    return {
        "visible": bool(raw.get("visible", False)),
        # None means "pick a round number that fits", decided at render time
        # against the panel's actual physical width.
        "target_um": (as_float(raw.get("target_um"), 0.0) or None),
        "unit": one_of(clean_text(raw.get("unit"), 8), SCALEBAR_UNITS, "auto"),
        "position": one_of(clean_text(raw.get("position"), 20),
                           PANEL_ANCHORS, "bottom_right"),
        "color": color(raw.get("color"), "#ffffff"),
        # Millimetres, like every other distance on a page. A bar thickness in
        # pixels would mean a different bar at every export DPI.
        "thickness_mm": clamp(as_float(raw.get("thickness_mm"), 0.8), 0.05, 20.0),
        "margin_mm": clamp(as_float(raw.get("margin_mm"), 1.2), 0.0, 50.0),
        "label": bool(raw.get("label", True)),
        "label_size_pt": _optional_size_pt(raw.get("label_size_pt")),
    }


def normalize_colorbar(raw):
    """An intensity ramp per visible channel, with ticks in RAW units.

    Ticks are labelled with the channel's own display window, which is the
    number the user set the contrast against and the number another lab would
    need to reproduce the picture. Anything else -- a 0-255 byte scale, a
    percentage -- would be a quantity the figure does not actually encode.

    Off by default. A colour bar is a claim that the intensities are
    quantitative, and most panels are not making it.
    """
    return {
        "visible": bool(raw.get("visible", False)),
        "orientation": one_of(clean_text(raw.get("orientation"), 20),
                              COLORBAR_ORIENTATIONS, "horizontal"),
        "position": one_of(clean_text(raw.get("position"), 20),
                           PANEL_ANCHORS, "bottom_left"),
        # The bar's SHORT dimension, whichever way it runs.
        "thickness_mm": clamp(as_float(raw.get("thickness_mm"), 1.6), 0.1, 40.0),
        # Between one channel's bar and the next, and between a bar and its own
        # tick labels -- one control, because two knobs for "space around the
        # bars" is a knob nobody can predict the effect of.
        "gap_mm": clamp(as_float(raw.get("gap_mm"), 1.0), 0.0, 40.0),
        "margin_mm": clamp(as_float(raw.get("margin_mm"), 1.2), 0.0, 50.0),
        # 0 means a bare ramp; 2 is the two ends of the window.
        "ticks": clamp(as_int(raw.get("ticks"), 2), 0, MAX_COLORBAR_TICKS),
        "tick_color": color(raw.get("tick_color"), "#ffffff"),
        "tick_width_pt": clamp(as_float(raw.get("tick_width_pt"), 0.5), 0.0, 20.0),
        "tick_length_mm": clamp(as_float(raw.get("tick_length_mm"), 0.8), 0.0, 20.0),
        "label_size_pt": _optional_size_pt(raw.get("label_size_pt")),
    }


def normalize_panel_label(raw):
    """One free caption drawn on a panel.

    It carries its own id so that editing the third of five is an update to
    that one rather than a rewrite of the list -- which is what makes reordering
    and deleting survive two people editing the same figure.
    """
    return {
        "label_id": clean_id(raw.get("label_id")) or "",
        "text": clean_text(raw.get("text")),
        "position": one_of(clean_text(raw.get("position"), 20),
                           PANEL_ANCHORS, "top_left"),
        "color": color(raw.get("color"), "#ffffff"),
        "size_pt": _optional_size_pt(raw.get("size_pt")),
        "bold": bool(raw.get("bold", False)),
        "italic": bool(raw.get("italic", False)),
    }


def _optional_size_pt(value):
    """A font size in points, or None for "the figure's".

    None is not the same as zero and not the same as a default written out: it
    is a live reference to `settings.style`, so changing the figure's body size
    moves everything that never asked for its own.
    """
    size = as_float(value, 0.0)
    if size <= 0:
        return None
    return clamp(size, 1.0, 400.0)


def normalize_placement(raw):
    """Where a panel sits ON THE PAGE, in millimetres.

    Deliberately separate from `scene.viewport`, which is where it sits IN THE
    IMAGE. Recropping the source and resizing the box on the page are different
    intents, and collapsing them is what makes a linked split-channel row
    impossible to keep in step.
    """
    return {
        "page_id": validate_id(raw.get("page_id"), "page id"),
        "x_mm": as_float(raw.get("x_mm"), 0.0),
        "y_mm": as_float(raw.get("y_mm"), 0.0),
        "w_mm": max(1.0, as_float(raw.get("w_mm"), 40.0)),
        "h_mm": max(1.0, as_float(raw.get("h_mm"), 40.0)),
        "z": as_int(raw.get("z"), 0),
    }


def normalize_scene(raw):
    """The captured viewer state -- everything that affects what is rendered.

    Deliberately NOT captured: sidebar scroll, cursor, open tooltips, status
    text. None of it changes a pixel of the science, and storing it would make
    two identical captures compare unequal.
    """
    raw = raw if isinstance(raw, dict) else {}
    version = raw.get("snapshot_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > SNAPSHOT_VERSION:
        raise UnreadableFigure(
            f"a panel in this figure was captured by a newer version of Plexora "
            f"(snapshot {version}, this build reads {SNAPSHOT_VERSION})"
        )

    viewport = raw.get("viewport") if isinstance(raw.get("viewport"), dict) else {}
    overlays = raw.get("core_overlays") if isinstance(raw.get("core_overlays"), dict) else {}

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "source_id": clean_id(raw.get("source_id")),
        # Full-resolution image pixels. Never CSS pixels, never viewport
        # fractions, never coordinates at whatever pyramid level happened to be
        # on screen -- those three are indistinguishable from these once written
        # down, and only one of them can be re-rendered at any DPI.
        "viewport": {
            "x": as_float(viewport.get("x"), 0.0),
            "y": as_float(viewport.get("y"), 0.0),
            "w": max(1.0, as_float(viewport.get("w"), 1.0)),
            "h": max(1.0, as_float(viewport.get("h"), 1.0)),
        },
        "channels": [normalize_scene_channel(c) for c in raw.get("channels") or []
                     if isinstance(c, dict) and clean_text(c.get("key"))],
        "core_overlays": {
            "cell_layers": [normalize_cell_layer(layer)
                            for layer in overlays.get("cell_layers") or []
                            if isinstance(layer, dict)],
            "hd_tiles": bool(overlays.get("hd_tiles", False)),
            "scalebar_visible": bool(overlays.get("scalebar_visible", False)),
        },
        # Opaque by contract: only the plugin that wrote a subtree reads it
        # back. Figure Builder stores the name, the version and the blob, and
        # the embedded `legend` so that export needs no live plugin JavaScript.
        "plugins": normalize_plugin_states(raw.get("plugins")),
        "captured_at": clean_text(raw.get("captured_at")),
    }


def normalize_scene_channel(raw):
    """One channel as it was displayed.

    Identified by `key` -- the URL key the tile route uses -- rather than by the
    marker name, because a name is something the user renames and a key is not.
    `fullname_at_capture` rides along for the legend and for the "this channel
    is gone" message; nothing resolves a channel by it.

    `window` is raw 16-bit, always, whatever domain the slider was in. It is
    what the project's own saved channel list stores, for the same reason: a
    byte-domain window means nothing once HD mode is on.
    """
    window = raw.get("window")
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        window = [0, 65535]
    rgb = raw.get("color") if isinstance(raw.get("color"), dict) else {}
    return {
        "key": clean_text(raw.get("key")),
        "fullname_at_capture": clean_text(raw.get("fullname_at_capture")),
        "color": {
            "r": clamp(as_int(rgb.get("r"), 255), 0, 255),
            "g": clamp(as_int(rgb.get("g"), 255), 0, 255),
            "b": clamp(as_int(rgb.get("b"), 255), 0, 255),
        },
        "window": [as_float(window[0], 0.0), as_float(window[1], 65535.0)],
        "visible": bool(raw.get("visible", True)),
    }


def normalize_cell_layer(raw):
    return {
        "name": clean_text(raw.get("name")),
        "mode": clean_text(raw.get("mode"), 20),
        "opacity": clamp(as_float(raw.get("opacity"), 1.0), 0.0, 1.0),
        "visible": bool(raw.get("visible", True)),
        "z": as_int(raw.get("z"), 0),
    }


def normalize_plugin_states(raw):
    """What each open plugin was drawing, stored opaquely.

    `state` and nothing else. A contribution used to carry a `legend` the
    plugin had computed at capture time, so that a panel could print a row per
    phenotype -- but the exported raster renders CHANNELS, so those rows
    described an overlay the deliverable did not contain, and whether a figure
    had them at all depended on which plugins were installed the day it was
    captured. They are dropped on read: an old figure loses rows it should
    never have had rather than failing to open.
    """
    out = {}
    for name, entry in (raw if isinstance(raw, dict) else {}).items():
        name = clean_text(name, 64)
        if not name or not isinstance(entry, dict):
            continue
        out[name] = {
            "version": clean_text(entry.get("version"), 64),
            "state": entry.get("state") if isinstance(entry.get("state"), (dict, list)) else {},
        }
    return out


# -- annotations and groups ---------------------------------------------


def normalize_annotation(raw):
    kind = clean_text(raw.get("type"), 20)
    if kind not in ANNOTATION_TYPES:
        raise ValueError(f"unknown annotation type {kind!r}")
    geometry = raw.get("geometry") if isinstance(raw.get("geometry"), dict) else {}
    style = raw.get("style") if isinstance(raw.get("style"), dict) else {}
    align = clean_text(style.get("align"), 8)
    valign = clean_text(style.get("valign"), 8)
    normalized = {
        "annotation_id": validate_id(raw.get("annotation_id"), "annotation id"),
        "type": kind,
        "page_id": validate_id(raw.get("page_id"), "page id"),
        "geometry": {
            "x_mm": as_float(geometry.get("x_mm"), 0.0),
            "y_mm": as_float(geometry.get("y_mm"), 0.0),
            "w_mm": as_float(geometry.get("w_mm"), 20.0),
            "h_mm": as_float(geometry.get("h_mm"), 10.0),
            "rotation": as_float(geometry.get("rotation"), 0.0),
        },
        "text": clean_text(raw.get("text"), MAX_TEXT_LENGTH),
        "style": {
            "color": color(style.get("color"), "#000000"),
            "fill": color(style.get("fill"), "") if style.get("fill") else "",
            "line_width_pt": clamp(as_float(style.get("line_width_pt"), 0.75), 0.0, 20.0),
            "font_size_pt": clamp(
                as_float(style.get("font_size_pt"), textmetrics.DEFAULT_TEXT_SIZE_PT),
                1.0, 200.0),
            "font_family": textmetrics.family(clean_text(style.get("font_family"), 20)),
            "align": align if align in ("left", "center", "right", "justify") else "left",
            "line_height": clamp(as_float(style.get("line_height"),
                                          textmetrics.LINE_HEIGHT), 0.8, 3.0),
            "valign": valign if valign in ("top", "middle", "bottom") else "top",
            "autofit": bool(style.get("autofit", True)),
            # Normalised for every kind, not only for shapes. `style` is
            # deep-merged by `_update_annotation`, so a key that exists for one
            # kind ends up in the stored dict for any annotation that ever
            # copies a style -- a conditional key here would only be a key that
            # is sometimes missing. Today just the shape renderers read it.
            "opacity": clamp(as_float(style.get("opacity"), 1.0), 0.0, 1.0),
            # The five keys that make a line a line, normalised for every kind
            # for exactly the reason `opacity` above is. Only the stroke
            # renderers read them.
            #
            # Every one of them COERCES rather than refusing. `normalize_document`
            # reads a ValueError as "drop this annotation", so raising on a name
            # from a newer build would delete the user's arrow instead of drawing
            # it plainly, which is the wrong way round.
            "line_style": one_of(style.get("line_style"), LINE_STYLES, "solid"),
            "start_head": one_of(style.get("start_head"), HEAD_STYLES, "none"),
            # THE one default in this schema that depends on the annotation's
            # kind, and it is load-bearing. An `arrow` drawn before heads were
            # configurable stored no head at all and every one of them has to
            # keep its barbs; a `line` has never had one and must not grow one.
            # After this both say which they are, explicitly, forever.
            "end_head": one_of(style.get("end_head"), HEAD_STYLES,
                               "open" if kind == "arrow" else "none"),
            # Zero means "size it from the pen", which is what every arrow that
            # predates this key wants -- see `strokegeom.head_size`.
            "head_size_pt": clamp(as_float(style.get("head_size_pt"), 0.0),
                                  0.0, MAX_HEAD_SIZE_PT),
            "edge": one_of(style.get("edge"), LINE_EDGES, "standard"),
        },
        "z": as_int(raw.get("z"), 0),
    }
    if kind == "text":
        # `rich` is the words; `text` is a projection of it, recomputed here and
        # never trusted from the caller. One gate, one answer -- the alternative
        # is two fields that drift the first time an operation updates one.
        #
        # Only a text annotation carries it. A `rich` key on a rectangle would
        # be a field with no meaning, and `_update_annotation`'s merge would
        # carry it forever once one arrived.
        normalized["rich"] = normalize_rich_text(normalized["text"], raw.get("rich"))
        normalized["text"] = plain_text(normalized["rich"])
    if kind == "shape":
        # Same decision as `rich` above, and for the same reason: the payload
        # hangs off the annotation only for the kind that has one, so the merge
        # in `_update_annotation` cannot carry a path onto a text box and then
        # keep it there forever.
        normalized["shape"] = normalize_shape(raw.get("shape"))
    return normalized


def normalize_shape(raw):
    """A shape annotation's path, as nodes in its own box.

    Node coordinates are normalised 0-1 against the mm box in `geometry`, so
    resizing a shape rewrites four numbers instead of every point, and a
    rotation stays a property of the box rather than something baked into the
    path. Handles are ABSOLUTE positions in that same space, not offsets from
    their anchor: an offset has to be re-derived every time a node moves, and
    the two representations look identical until one of them is wrong.

    Every shape is nodes, including the ones the picker calls "rect" and
    "ellipse". `preset` is a label carried for the icon grid and for creation
    defaults; nothing ever reconstructs geometry from it. That is what keeps
    one renderer and one point editor, rather than a parametric path beside a
    vector one and a conversion between them.

    Additive beside the annotation types that came before it, and deliberately
    NOT a `SCHEMA_VERSION` bump -- the `rich` precedent. A build that predates
    the shape tool drops these annotations (`normalize_document` skips what
    this module refuses) and opens the rest of the figure; bumping the version
    instead would make the WHOLE figure refuse to open in every build already
    installed, to prevent losing the shapes that build could not draw anyway.

    Never raises, for the reason `normalize_rich_text` never does: the document
    loop reads a ValueError as "drop this annotation", so a raise here would
    silently delete the user's shape on the next read. Garbage becomes the unit
    rectangle instead, which is visible on the page and editable.
    """
    raw = raw if isinstance(raw, dict) else {}
    preset = clean_text(raw.get("preset"), 32)
    if preset not in SHAPE_PRESETS:
        preset = "custom"
    closed = bool(raw.get("closed", True))

    nodes = []
    entries = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    for entry in entries[:MAX_SHAPE_NODES]:
        if not isinstance(entry, dict):
            continue
        nodes.append({
            "x": _shape_coord(entry.get("x")),
            "y": _shape_coord(entry.get("y")),
            # Anything that is not the word "smooth" is a corner. A node type is
            # a promise about the two handles either side of it, and the weaker
            # promise is the safe one to guess.
            "type": "smooth" if clean_text(entry.get("type"), 8) == "smooth" else "corner",
            "in": _shape_handle(entry.get("in")),
            "out": _shape_handle(entry.get("out")),
        })

    # A closed path needs three nodes to enclose anything and an open one needs
    # two to go anywhere. Below that there is no shape to keep, so the fallback
    # stands in rather than an empty path that draws nothing and cannot be
    # grabbed to fix.
    if len(nodes) < (3 if closed else 2):
        return _unit_shape(preset)
    return {"preset": preset, "closed": closed, "nodes": nodes}


def _shape_coord(value):
    """One node or handle coordinate, in box space.

    Clamped well outside [0, 1] rather than into it: a control point legally
    sits past its anchor, and a node dragged outside the box is normal -- the
    client renormalises the box around it and the numbers come back. The bound
    exists to stop a runaway client writing 1e30 into a document, so the server
    never rescales anything, it only refuses the absurd.
    """
    return clamp(as_float(value, 0.0), -SHAPE_COORD_SLACK, 1.0 + SHAPE_COORD_SLACK)


def _shape_handle(raw):
    """A bezier control point, or None when the segment beside it is straight.

    An unreadable coordinate becomes None rather than 0.0: a handle at the
    origin is a curve yanked to the corner of the box, where absent is the
    honest answer and renders as the straight segment the node already implies.
    """
    if not isinstance(raw, dict):
        return None
    if as_float(raw.get("x"), None) is None or as_float(raw.get("y"), None) is None:
        return None
    return {"x": _shape_coord(raw.get("x")), "y": _shape_coord(raw.get("y"))}


def _unit_shape(preset):
    """What a malformed shape becomes: its own box, four corners."""
    return {
        "preset": preset if preset in SHAPE_PRESETS else "rect",
        "closed": True,
        "nodes": [{"x": x, "y": y, "type": "corner", "in": None, "out": None}
                  for x, y in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))],
    }


def normalize_rich_text(flat, raw=None):
    """A text annotation's words, as lines of styled runs.

    Additive beside `text`, and deliberately NOT a `SCHEMA_VERSION` bump. The
    precedent is `visual_groups`: a document written before this key existed
    simply has none, and a build that predates the key drops it and keeps the
    words. Bumping the version instead would make every figure that has ever
    held a text box refuse to open in every build already installed, to prevent
    a loss whose whole content is "the bold went away". The degradation is
    silent -- there is no marker saying this text was once formatted -- and a
    field whose only job is to describe another field would be worse.

    A LINE, not a paragraph, is the unit. Only the browser can measure a string,
    so only the browser can decide where a line breaks, and the break it chose
    is therefore stored rather than recomputed by an exporter that would have to
    guess. `hard` records whose break it is: True when the author pressed Enter,
    False when the wrap put it there and a later re-wrap may move it.

    One consequence to be honest about: a figure edited through the REST surface
    with no browser present keeps the breaks it had, even if the box was resized
    in the same edit. The words stay right and the breaks go stale, which is
    visible on the page rather than silently wrong in the PDF.

    Never raises. `normalize_document` skips an annotation whose normaliser
    raises, so a ValueError here would silently DELETE the user's text box on
    the next read -- oversized input is truncated instead.
    """
    lines = raw.get("lines") if isinstance(raw, dict) else None
    if not isinstance(lines, list):
        return _rich_from_plain(flat)

    budget = MAX_TEXT_LENGTH
    runs_left = MAX_TEXT_RUNS
    out = []
    for raw_line in lines[:MAX_TEXT_LINES]:
        if budget <= 0 or runs_left <= 0:
            break
        if not isinstance(raw_line, dict):
            continue
        raw_runs = raw_line.get("runs")
        runs = []
        # Not sliced: the list is already parsed and in memory, and every run
        # that survives costs at least one character, so the character budget
        # below is what bounds the output. Slicing the INPUT instead would drop
        # a caption that happened to arrive behind a long tail of empty spans.
        for raw_run in (raw_runs if isinstance(raw_runs, list) else []):
            if budget <= 0:
                break
            run = _normalize_run(raw_run, budget)
            if run is None:
                continue
            budget -= len(run["text"])
            runs.append(run)
        # Coalesce BEFORE capping. A word processor marks up every letter, so a
        # pasted sentence arrives as one run per character -- capping first
        # would throw away the tail of the sentence, which is precisely the
        # input coalescing exists to absorb.
        runs = _cap_runs(_coalesce_runs(runs), min(MAX_RUNS_PER_LINE, max(0, runs_left)))
        runs_left -= len(runs)
        # An empty line in the middle is content -- it is the blank line between
        # two paragraphs of a caption -- so it is kept rather than dropped.
        out.append({"hard": bool(raw_line.get("hard", True)), "runs": runs})

    while out and not out[-1]["runs"]:
        out.pop()
    if not out:
        return _rich_from_plain("")
    # Nothing sits above the first line for it to have wrapped from, so its
    # break is the author's by definition. Left as read, a `hard: false` first
    # line would make a re-wrap try to join it to the line before it.
    out[0]["hard"] = True
    return {"lines": out}


def plain_text(rich):
    """The flat string a rich text reads as. The `text` field is this."""
    lines = rich.get("lines") if isinstance(rich, dict) else None
    if not isinstance(lines, list):
        return ""
    return "\n".join(
        "".join(run["text"] for run in line["runs"]) for line in lines
    )[:MAX_TEXT_LENGTH]


def _rich_from_plain(flat):
    """Lines from a flat string -- the path every pre-`rich` document takes."""
    text = _normalize_breaks(flat if isinstance(flat, str) else "")
    out = []
    for raw_line in text.split("\n")[:MAX_TEXT_LINES]:
        cleaned = _clean_run_text(raw_line, MAX_TEXT_LENGTH)
        out.append({"hard": True, "runs": [{"text": cleaned}] if cleaned else []})
    return {"lines": out or [{"hard": True, "runs": []}]}


def _normalize_run(raw, budget):
    """One styled span, or None if there is nothing left of it.

    A run carries only what it OVERRIDES. An absent `size_pt` means "whatever
    the box says", so raising the box's font size still reaches every run the
    user never touched; writing the resolved value onto every run instead would
    freeze each one at the size it happened to be created at.
    """
    if not isinstance(raw, dict):
        return None
    text = _clean_run_text(raw.get("text"), budget)
    if not text:
        return None
    run = {"text": text}
    for mark in ("bold", "italic", "underline", "strike"):
        if raw.get(mark):
            run[mark] = True
    name = clean_text(raw.get("family"), 20)
    if name in textmetrics.FAMILIES:
        run["family"] = name
    size = as_float(raw.get("size_pt"), None)
    if size is not None:
        run["size_pt"] = clamp(size, 1.0, 200.0)
    if isinstance(raw.get("color"), str) and COLOR_PATTERN.match(raw["color"]):
        run["color"] = raw["color"].lower()
    return run


def _coalesce_runs(runs):
    """Adjacent runs with identical marks become one.

    Load-bearing, not tidiness. A contenteditable emits a fresh span per
    keystroke, so without this a caption grows a run per character: the document
    balloons, the "did anything change?" check before a commit never fires
    because the shape differs every time, and the client and server normalisers
    can no longer be compared for equality at all. Coalescing is what makes the
    stored form canonical.
    """
    out = []
    for run in runs:
        if out and _run_marks(out[-1]) == _run_marks(run):
            out[-1]["text"] += run["text"]
        else:
            out.append(run)
    return out


def _cap_runs(runs, cap):
    """At most `cap` runs, keeping every word.

    The overflow is folded into the last surviving run rather than sliced off.
    A line with more than a hundred DISTINCTLY styled spans is already beyond
    anything a caption needs, but the words on it are still the user's -- so the
    cap costs them the marks past that point and never the sentence. Same trade
    the whole feature makes when it degrades.
    """
    if cap <= 0:
        return []
    if len(runs) <= cap:
        return runs
    kept = runs[:cap]
    kept[-1] = {**kept[-1], "text": kept[-1]["text"] + "".join(
        run["text"] for run in runs[cap:])}
    return kept


def _run_marks(run):
    return tuple(run.get(key) for key in
                 ("bold", "italic", "underline", "strike", "family", "size_pt", "color"))


def _normalize_breaks(value):
    """CRLF and CR become LF, tabs become a space.

    `_CONTROL_CHARS` deliberately lets \\t, \\n and \\r through, so a paste out of
    a Windows word processor otherwise leaves a bare CR sitting inside a run,
    where it is a line break that the line list knows nothing about.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")


def _clean_run_text(value, budget):
    """Run text, capped -- and NOT stripped.

    `clean_text` strips, which is right for a title and wrong here: the spaces
    around a run are the gaps between it and its neighbours on the same line,
    so stripping them closes up "Fig. 1a" + "  DAPI" into "Fig. 1aDAPI".
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _RUN_CONTROL_CHARS.sub("", value.replace("\t", " "))[:max(0, budget)]


def normalize_link_group(raw):
    sync = [s for s in (raw.get("sync") or []) if s in ("viewport", "crop", "size", "channels")]
    return {
        "group_id": validate_id(raw.get("group_id"), "group id"),
        "panel_ids": [clean_id(p) for p in raw.get("panel_ids") or [] if clean_id(p)],
        "sync": sync or ["viewport"],
    }


def normalize_group(raw):
    """A visual group: things that move, and are selected, together.

    NOT a link group, and the distinction is the reason there are two. A link
    group says "these panels show the same field at the same size", is created
    by Split Composite, and its whole job is to propagate an edit from one
    member to the others. A visual group says "an image and its title are one
    object as far as clicking and dragging are concerned", is created by the
    user pressing Cmd+G, and propagates nothing.

    They also hold different things. A link group holds panels, because
    synchronising a viewport onto a text box is meaningless; a visual group
    holds panels AND annotations, because image-plus-caption is the commonest
    reason to want one.

    Reusing link_groups for this would have meant a group whose `sync` list is
    empty behaving as a different feature -- and the split rows already in
    people's figures occupying the key.
    """
    return {
        "group_id": validate_id(raw.get("group_id"), "group id"),
        "member_ids": [clean_id(m) for m in raw.get("member_ids") or [] if clean_id(m)],
    }



# -- primitives ---------------------------------------------------------


def validate_id(value, what):
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def clean_id(value):
    """An id if it is one, "" otherwise -- for fields where absence is legal."""
    return value if isinstance(value, str) and ID_PATTERN.match(value) else ""


def validate_figure_id(value):
    """A figure id that is safe to use as a directory name, or a ValueError.

    Checked at every entry point rather than once at creation: this value
    arrives from a URL, and it is joined onto a filesystem path.
    """
    if not isinstance(value, str) or not FIGURE_ID_PATTERN.match(value):
        raise ValueError(f"invalid figure id: {value!r}")
    return value


def clean_text(value, limit=MAX_TITLE_LENGTH):
    """User-typed text, made safe to store and hand back.

    Control characters go and length is capped. Nothing is HTML-escaped here --
    escaping at the store double-escapes on every round trip, and the place that
    has to be careful is the one putting it in the DOM.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _CONTROL_CHARS.sub("", value).strip()[:limit]


def color(value, fallback="#000000"):
    if isinstance(value, str) and COLOR_PATTERN.match(value):
        return value.lower()
    return fallback


def as_int(value, fallback):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    try:
        return int(value)
    except (ValueError, OverflowError):
        return fallback


def as_float(value, fallback):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    value = float(value)
    # NaN and infinity survive json.loads and then poison every arithmetic they
    # touch -- a panel at x=NaN simply never draws, with no error anywhere.
    if value != value or value in (float("inf"), float("-inf")):
        return fallback
    return value


def one_of(value, allowed, fallback):
    """A name out of a fixed vocabulary, or the fallback.

    Never raises. A name this build does not know is what a document written by
    a newer one looks like from here, and drawing the annotation with a plain
    setting beats `normalize_document` deleting it.
    """
    name = clean_text(value, 20)
    return name if name in allowed else fallback


def clamp(value, low, high):
    return max(low, min(high, value))
