"""Reference sheets: labelled library tiles laid out for a model to compare against.

A sheet is one picture, shown once to each conversation that answers looks
(`SessionOptions.reference_sheet`): the six classes of `library.CLASSES` for
each shape of stain, so the reader has, beside every look, how faint a stain
can be against its own background and still be a cell's own, what spill and
debris look like, and what own stain is for a ring, a fill, a fibre or a
dendrite. Layouts, so sheets can be compared:

- `by_shape` (the default): a row per shape (`schemas.STAIN_SHAPES` order),
  a column per class; dense and sparse tiles alternate along the row; a class
  with no tile is a dark slot that says so.
- `pairs`: per shape, faint and limit positives each beside the negative
  closest to them in brightness (same marker and density preferred) --
  matched contrasts, which teach a boundary better than examples alone
  (counterfactual pairs, Xiong et al. CVPR 2026).
- `by_marker`: a row per marker, a column per class.

Deterministic: the same labelled tiles, layout and seed draw the same bytes
and the same `digest` (what a session's memo key carries). Capped at
`MAX_WIDTH` x `MAX_HEIGHT`, `MAX_ROWS` rows of `MAX_PER_ROW` tiles; what does
not fit is dropped in a fixed order and listed in `dropped`.
"""

from __future__ import annotations

import hashlib
import io
import time

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.gating.server.autogate import library, schemas

SHEET_RENDERER = "plexora.gating.library_sheet/1"
LAYOUTS = ("by_shape", "pairs", "by_marker")
MAX_WIDTH, MAX_HEIGHT = 1024, 768
MAX_ROWS, MAX_PER_ROW = 6, 6
TITLE = ("REFERENCE · blue nuclei, yellow marker · each tile = morphology | context · ticks "
         "mark the judged cell · faint and limit ARE positives")
#: The words a sheet's column header and captions use for each class.
CLASS_WORDS = {"strong": "strong +", "faint": "faint +", "limit": "limit +",
               "negative": "negative", "spill": "spill", "artifact": "artifact"}


def _geometry():
    from plexora.agent.evidence import collage

    return {"header": collage.HEADER_H, "row_header": collage.ROW_HEADER_H,
            "caption": collage.CAPTION_H, "gap": collage.GAP,
            "tile": library.TILE_SIZE}


def _pick(candidates, rng):
    """One of `candidates` (summaries sorted by id), by a seeded draw."""
    if not candidates:
        return None
    return candidates[int(rng.integers(len(candidates)))]


def plan(labelled, *, layout="by_shape", shapes=None, markers=None, per_class=3, seed=0):
    """(rows, dropped): [{label, tiles: [summary | {"empty": class}], heads?}]
    for the layout, from labelled tile summaries."""
    if layout not in LAYOUTS:
        raise AgentError("invalid_input", f"layout is one of {LAYOUTS}")
    pool = sorted((t for t in labelled if t.get("class") in library.CLASSES),
                  key=lambda t: t["tile_id"])
    if shapes:
        pool = [t for t in pool if t.get("shape") in shapes]
    if markers:
        pool = [t for t in pool if t.get("marker") in markers]
    rows, dropped = [], []
    if layout in ("by_shape", "by_marker"):
        key = "shape" if layout == "by_shape" else "marker"
        if layout == "by_shape":
            order = [s for s in schemas.STAIN_SHAPES + ("unknown",)
                     if any(t.get("shape") == s for t in pool)]
        else:
            order = sorted({t.get("marker") for t in pool if t.get("marker")})
        for r, name in enumerate(order):
            mine = [t for t in pool if t.get(key) == name]
            tiles = []
            for c, klass in enumerate(library.CLASSES):
                want = "dense" if c % 2 == 0 else "sparse"
                of_class = [t for t in mine if t["class"] == klass]
                preferred = [t for t in of_class if t.get("density") == want] or of_class
                chosen = _pick(preferred, np.random.default_rng([int(seed), r, c]))
                tiles.append(chosen if chosen else {"empty": klass})
            rows.append({"label": str(name).replace("_", " "), "tiles": tiles,
                         "heads": True})
    else:
        shape_order = [s for s in schemas.STAIN_SHAPES + ("unknown",)
                       if any(t.get("shape") == s for t in pool)]
        for r, shape in enumerate(shape_order):
            mine = [t for t in pool if t.get("shape") == shape]
            negatives = [t for t in mine if t["class"] == "negative"]
            positives = [t for t in mine if t["class"] in ("faint", "limit")]
            rng = np.random.default_rng([int(seed), r, 0])
            positives = sorted(positives, key=lambda t: (t["class"] != "limit",
                                                         t["tile_id"]))
            if len(positives) > per_class:
                keep = sorted(rng.choice(len(positives), size=per_class, replace=False))
                positives = [positives[int(i)] for i in keep]
            tiles, used = [], set()
            for pos in positives:
                rest = [n for n in negatives if n["tile_id"] not in used]
                if not rest:
                    break
                rest.sort(key=lambda n: (n.get("marker") != pos.get("marker"),
                                         n.get("density") != pos.get("density"),
                                         abs(float(n.get("pct") or 0) - float(pos.get("pct")
                                                                               or 0)),
                                         n["tile_id"]))
                used.add(rest[0]["tile_id"])
                tiles += [pos, rest[0]]
            if tiles:
                rows.append({"label": f"{shape.replace('_', ' ')}: positive | closest "
                                      "negative", "tiles": tiles, "heads": False})
    for row in rows[MAX_ROWS:]:
        dropped.append({"row": row["label"], "reason": "more rows than a sheet holds"})
    rows = rows[:MAX_ROWS]
    for row in rows:
        if len(row["tiles"]) > MAX_PER_ROW:
            for t in row["tiles"][MAX_PER_ROW:]:
                dropped.append({"row": row["label"], "tile_id": t.get("tile_id"),
                                "reason": "more tiles than a row holds"})
            row["tiles"] = row["tiles"][:MAX_PER_ROW]
    return rows, dropped


def _size(rows):
    g = _geometry()
    tw, th = g["tile"]
    per = max([len(r["tiles"]) for r in rows] + [1])
    width = per * tw + (per - 1) * g["gap"]
    heads = any(r.get("heads") for r in rows)
    height = g["header"] + (g["row_header"] if heads else 0) + \
        len(rows) * (g["row_header"] + th + g["caption"] + g["gap"])
    return width, height


def render(rows, *, caption=None):
    """(PIL image, drawn tile ids) for planned rows."""
    from PIL import Image, ImageDraw

    from plexora.agent.evidence import collage

    g = _geometry()
    tw, th = g["tile"]
    width, height = _size(rows)
    if width > MAX_WIDTH or height > MAX_HEIGHT:
        raise AgentError("internal_error", f"a sheet of {width} x {height} is past the "
                         f"{MAX_WIDTH} x {MAX_HEIGHT} cap")
    canvas = Image.new("RGB", (width, height), collage.BG)
    draw = ImageDraw.Draw(canvas)
    font, small = collage._font(11), collage._font(10)
    draw.rectangle((0, 0, width, g["header"] - 1), fill=collage.HEADER_BG)
    draw.text((4, 1), caption or TITLE, fill=collage.TEXT, font=font)
    y = g["header"]
    if any(r.get("heads") for r in rows):
        for c, klass in enumerate(library.CLASSES):
            draw.text((c * (tw + g["gap"]) + 2, y), CLASS_WORDS[klass],
                      fill=collage.POSITIVE_TEXT if klass in library.POSITIVE_CLASSES
                      else collage.DIM, font=small)
        y += g["row_header"]
    drawn = []
    for row in rows:
        draw.text((3, y), row["label"], fill=collage.DIM, font=small)
        y += g["row_header"]
        for c, tile in enumerate(row["tiles"]):
            x = c * (tw + g["gap"])
            if tile.get("empty"):
                draw.rectangle((x, y, x + tw - 1, y + th - 1), fill=(24, 26, 31))
                draw.text((x + 6, y + th // 2 - 6), f"no {tile['empty']}", fill=collage.DIM,
                          font=small)
                continue
            png = library.tile_png(tile["tile_id"])
            if png is None:
                draw.rectangle((x, y, x + tw - 1, y + th - 1), fill=(24, 26, 31))
                continue
            canvas.paste(Image.open(io.BytesIO(png)).convert("RGB").resize((tw, th)), (x, y))
            words = CLASS_WORDS[tile["class"]] if not row.get("heads") else ""
            text = f"{words} {tile.get('marker') or ''}".strip()[:26]
            draw.text((x + 2, y + th), text, fill=collage.POSITIVE_TEXT
                      if tile["class"] in library.POSITIVE_CLASSES else collage.TEXT,
                      font=small)
            drawn.append(tile["tile_id"])
        y += th + g["caption"] + g["gap"]
    return canvas, drawn


def build(name, *, layout="by_shape", shapes=None, markers=None, per_class=3, seed=0,
          caption=None) -> dict:
    """Build (or rebuild) the sheet `name` from the labelled tiles: writes
    `sheets/<name>.webp|.png|.json` and returns the JSON with `image`."""
    from plexora.agent.evidence import collage
    from plexora.agent.sessions import budget as budgets
    from plexora.server.utils import fast_png

    if not library._NAME.match(str(name or "")):
        raise AgentError("invalid_input", "a sheet's name is 1 to 40 letters, digits, _ or -")
    base = library.ensure()
    labelled, warnings = library.tiles(labelled=True)
    rows, dropped = plan(labelled, layout=layout, shapes=shapes, markers=markers,
                         per_class=per_class, seed=seed)
    if not any(t.get("tile_id") for r in rows for t in r["tiles"]):
        raise AgentError("precondition_missing",
                         "no labelled tile matches: sample some cells (library_sample), sort "
                         "them into the inbox's class folders, then library_ingest",
                         detail={"labelled": len(labelled), "layout": layout,
                                 "warnings": warnings[:10]})
    canvas, drawn = render(rows, caption=caption)
    png = fast_png.encode_rgb8_png(np.asarray(canvas))
    webp, _fmt = collage.encode(canvas, "webp")
    digest = hashlib.sha256("|".join([*drawn, layout, SHEET_RENDERER, caption or TITLE])
                            .encode("utf-8")).hexdigest()[:16]
    meta = {"name": name, "layout": layout, "renderer": SHEET_RENDERER,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "built_at_s": time.time(), "size": [canvas.width, canvas.height],
            "tile_ids": drawn, "rows": [{"label": r["label"], "tiles": [
                t.get("tile_id") or f"empty:{t.get('empty')}" for t in r["tiles"]]}
                for r in rows],
            "digest": digest, "caption": caption or TITLE,
            "params": {"shapes": shapes, "markers": markers, "per_class": per_class,
                       "seed": seed},
            "dropped": dropped, "warnings": warnings[:20],
            "estimated_vision_tokens": budgets.vision_tokens(canvas.width * canvas.height)}
    folder = base / "sheets"
    library._atomic(folder / f"{name}.png", png)
    library._atomic(folder / f"{name}.webp", webp)
    library._write_json(folder / f"{name}.json", meta)
    return {**meta, "image": webp, "format": "webp"}
