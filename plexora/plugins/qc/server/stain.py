"""Low quality staining: did each marker's stain work?

One picture per marker channel, read by a vision model for whether the
channel shows a real, specific population of cells (`real`), a real one at
low contrast (`weak`), or none (`failed`). The picture is the one the owner's
labels of 209 channels on seven images were matched against (2026-11-23,
docs/internal/bench/runs/2026-11-23-channel-labels): a person judged a stain
from single cells and how they sit in the tissue, at several scales, and from
co-expression -- never from the whole slide alone, and never from its
brightest spots, which on CyCIF were mostly artefacts.

    A  the whole tissue (white boxes: B and C; a cyan circle: D)
    B  a tissue-scale field of typical cell density
    C  a second one, far from B -- B and C are the same two places for every
       channel of the image, chosen by the nuclear stain, never by the marker
    D  a few cells round one cell of the marker's top 0.5 % by its table
       value, its top 0.05 % left out (often debris)
    E  co-expression with a partner from the marker graph, in a field holding
       the most of the partner's top-decile cells: the marker alone, the
       partner alone, and a merge (marker green above its own floor in that
       field, partner magenta, both white)

Marker yellow over dim blue nuclei; contrast per channel (0.5th-99.8th
percentile of the tissue and the two fields), D widened -- never narrowed --
when its cells are brighter than that.

A `failed` first reading is read again by a second reader that never saw it
(`STAIN["rereads"]`); when the two differ, a third, blind to both, decides
(best of three, `settle`). On the bench (Sonnet, two readers, row E): 7 of 9 failures,
5 real and 5 weak stains of 200 called failed -- so a verdict here is a flag
for a person, never a removal: nothing about a channel's cells or its gate
changes because of it.
"""

from __future__ import annotations

import io
import re

import numpy as np

from plexora.plugins.qc.server import schemas

#: Bumped when the picture or the question changes: memo answers to the old
#: pictures are not replayed (`memo_versions`).
VERSION = "1"
RENDERER = "plexora.qc.stain/2"
KIND = "qc_stain"
VERDICTS = ("real", "weak", "failed", "cannot_tell")
#: What the Low quality staining section lists.
LOW_QUALITY = ("weak", "failed")
STAIN = schemas.STAIN

#: Channels that are not a stain of a marker: autofluorescence, blanks,
#: controls and empty or background cycles. Read on the plain name.
NON_MARKER = re.compile(r"^(af\d*|autofluo\w*|blank\w*|empty\w*|background\w*|bg\d*|"
                        r"control\w*|argo\d+\w*|\w*_blank|area)$", re.I)

CONFIDENCE_RANK = {"high": 0, "moderate": 1, "low": 2}


# -- which channels, which partner ------------------------------------------------------


def checked_channels(names, nuclear=()) -> list:
    """The marker channels a session reads: every channel but the nuclear
    stains and those `NON_MARKER` names."""
    from plexora.ai import vocabulary

    nuclear = set(nuclear or ())
    out = []
    for name in names:
        if name in nuclear:
            continue
        plain = vocabulary.plain_name(name)
        if NON_MARKER.match(str(plain)) or NON_MARKER.match(str(name)):
            continue
        out.append(name)
    return out


def _panel(names):
    """{panel name: (canonical, vocabulary entry)} and {canonical: [names]}."""
    from plexora.ai import vocabulary

    entries = vocabulary.load()["entries"]
    resolved = vocabulary.resolve_panel(list(names))
    info, by_canonical = {}, {}
    for name, canonical in resolved.items():
        if canonical:
            info[name] = (canonical, entries.get(canonical) or {})
            by_canonical.setdefault(canonical, []).append(name)
    return info, by_canonical


def _stated(info, by_canonical, name):
    """[(panel name, relation, confidence, lineage)] the vocabulary states for `name`."""
    from plexora.ai import vocabulary

    _canonical, entry = info.get(name, (None, {}))
    out = []
    for partner in entry.get("partners") or []:
        for other in by_canonical.get(partner.get("marker"), []):
            if other != name:
                out.append((other, partner.get("relation"),
                            partner.get("confidence") or vocabulary.DEFAULT_CONFIDENCE,
                            partner.get("lineage")))
    return out


#: A child population that is a state readout (FOXP3 or PD-1 within CD4) is no
#: partner: it says nothing of where the marker's own cells are, and a dim
#: state readout makes row E read as the marker failing (gating's
#: `hierarchy.STATE_ROLES`, read from the vocabulary here).
STATE_ROLES = ("state", "signalling")


def _unique(cands):
    seen, out = set(), []
    for name, words in cands:
        if name not in seen:
            seen.add(name)
            out.append((name, words))
    return out


def partner(names, marker, exclude=()) -> tuple:
    """(partner, words) for row E, or (None, None). Only the marker graph
    decides. The CARRIERS -- the populations the marker's cells sit in -- come
    first: its forward `subset` / `coexpressed` partners (strongest first), a
    marker stating it is co-expressed with this one, then the entry's
    `subset_any` carriers in the order it lists them. Of the carriers, any one
    another carrier sits inside (a stated `subset` of it) is dropped, so the
    partner is the most specific population the marker lives in: CD4 for
    TIGIT, CD3 for CD4, never CD45 beside them. Only a marker with no carrier
    on the panel takes a CHILD -- a marker stated as a subset of it -- and
    never a state readout (`STATE_ROLES`): FOXP3 says nothing of where CD4's
    cells are."""
    exclude = set(exclude or ()) | {marker}
    info, by_canonical = _panel(names)
    stated = {n: _stated(info, by_canonical, n) for n in info}
    own = [s for s in stated.get(marker, []) if s[0] not in exclude]
    carriers = []
    for other, relation, confidence, _lin in sorted(
            (s for s in own if s[1] in ("subset", "coexpressed")),
            key=lambda s: CONFIDENCE_RANK.get(s[2], 1)):
        carriers.append((other, f"{marker} {'within' if relation == 'subset' else 'with'} "
                                f"{other} (marker graph, {confidence})"))
    children = []
    for other, rows in stated.items():
        if other in exclude:
            continue
        for target, relation, _conf, _lin in rows:
            if target == marker and relation == "coexpressed":
                carriers.append((other, f"{marker} with {other} (marker graph)"))
            elif target == marker and relation == "subset" \
                    and (info.get(other, (None, {}))[1].get("role") not in STATE_ROLES):
                children.append((other, f"{other} cells are a subset of {marker} "
                                        "(marker graph)"))
    for other, relation, _conf, _lin in own:
        if relation == "subset_any":
            carriers.append((other, f"{marker} on a subset of {other} cells (marker graph)"))
    pool = _unique(carriers) or _unique(children)
    if not pool:
        return None, None

    def inside(q, p):
        return any(t == p and r == "subset" for t, r, _c, _l in stated.get(q, []))

    specific = [c for c in pool if not any(o[0] != c[0] and inside(o[0], c[0])
                                           for o in pool)]
    return (specific or pool)[0]


def note(names, marker) -> dict:
    """What the vocabulary says of how the marker is expressed (compartment,
    lineage, expression, morphology), for the reader's expectation."""
    info, _ = _panel(names)
    _canonical, entry = info.get(marker, (None, {}))
    return {k: entry.get(k) for k in ("compartment", "lineage", "expression", "morphology")
            if entry.get(k)}


def evidence(names, marker, project, manifest) -> dict:
    """A stain packet's evidence beside its picture's `manifest`: the
    packet builder's and the stain bench's (`plexora.ai.stain_bench`), so
    the bench reads what a session sends."""
    return {"marker": marker, "project": project,
            "expression": note(names, marker),
            "partner": manifest.get("partner"),
            "partner_relation": manifest.get("partner_words"),
            "field_um": manifest.get("field_um"), "cells_um": manifest.get("cells_um")}


def settle(verdicts, *, rereads=True):
    """The channel's verdict from its readings, or None when one more is due.
    A first `failed` is read again by a second, blind reader (when
    `rereads`); two `failed` stand. When the second disagrees, a third, blind
    to both, decides -- best of three: `failed` when two of three say so,
    else the verdict two share, else (all three differ) `weak` when one said
    weak, else `cannot_tell`."""
    verdicts = [v for v in verdicts if v]
    if not verdicts:
        return None
    if len(verdicts) == 1:
        if verdicts[0] == "failed" and rereads:
            return None
        return verdicts[0]
    if len(verdicts) == 2:
        if verdicts[1] == "failed":
            return "failed"
        return None if rereads else verdicts[1]
    three = verdicts[:3]
    for verdict in ("failed", *VERDICTS):
        if three.count(verdict) >= 2:
            return verdict
    return "weak" if "weak" in three else "cannot_tell"


# -- the picture ---------------------------------------------------------------------------


class _Image:
    """What every channel's picture of one image shares: the levels, the
    nuclear plane, the tissue, B and C, the cells. Built once per image and
    reused while its inputs stand."""

    def __init__(self, session, project):
        from plexora import api
        from plexora.agent import presets
        from plexora.plugins.qc.server import plane as qc_plane
        from plexora.server.utils import pixel_scale, source_image

        self.session, self.project = session, project
        record = session.project(project)
        channels = list(record.image.real_channels)
        self.names = [c.get("fullname") or c.get("name") for c in channels]
        self.keys = {n: source_image.channel_key(c) for n, c in zip(self.names, channels)}
        nuclear = presets.nuclear_channels(self.names)
        if not nuclear:
            raise ValueError("no nuclear channel is recognisable by name")
        self.nuclear = nuclear[0]
        pixel = pixel_scale.pixel_size(record)
        self.pixel_um = float(pixel["value"]) if pixel else None
        self.image_data = session.image_data(project)
        with qc_plane.reader(self.image_data) as src:
            self.index = {n: src.channel_index(self.keys[n]) for n in self.names}
            self.h0, self.w0 = src.level_shape(0)
            levels = max(1, int(src.levels))
            self.shapes = [src.level_shape(level) for level in range(levels)]
            self.ov = next((lv for lv, (h, w) in enumerate(self.shapes)
                            if max(h, w) <= STAIN["overview_max_px"]), levels - 1)
            # The field: FIELD_UM across at the coarsest level giving at
            # least `field_min_px` pixels.
            field0 = (STAIN["field_um"] / self.pixel_um if self.pixel_um
                      else STAIN["field_px_no_pixel"])
            self.field0 = float(field0)
            self.cl = 0
            for lv in range(levels):
                if self.field0 / self.factor(lv) >= STAIN["field_min_px"]:
                    self.cl = lv
            self.cpx = int(round(self.field0 / self.factor(self.cl)))
            self.cells0 = int(round(STAIN["cells_um"] / self.pixel_um if self.pixel_um
                                    else STAIN["cells_px_no_pixel"]))
            self.dna_ov = self._plane(src, self.nuclear, self.ov)
        self.tissue, self.boxes = self._fields()
        self.dna_crops = self._crops(self.nuclear)
        self.nlo, self.nhi = self._range(self.dna_ov, self.dna_crops, (0.01, 0.995))
        try:
            self.ds = api.project_data(project)
            self._cells()
        except Exception:  # noqa: BLE001 -- no table: D falls back to pixels, no row E
            self.ds, self.xy, self.markers = None, None, []

    def factor(self, level):
        h, w = self.shapes[level]
        return self.w0 / max(1, w)

    def _plane(self, src, name, level, box=None):
        h, w = self.shapes[level]
        box = box or (0, 0, w, h)
        plane, _ = src.read(self.index[name], level, box)
        return np.asarray(plane, dtype=np.float32)

    def _fields(self):
        from scipy import ndimage

        dna = self.dna_ov
        fac = self.factor(self.ov) / self.factor(self.cl)    # crop px per overview px
        side = max(3, int(round(self.cpx / fac)))
        sm = ndimage.uniform_filter(dna, size=side)
        H, W = dna.shape
        half = side // 2 + 1
        inner = np.zeros_like(sm, bool)
        inner[half:max(half + 1, H - half), half:max(half + 1, W - half)] = True
        tissue = sm > np.quantile(sm, 0.3)
        pool = sm[tissue & inner]
        if pool.size == 0:
            pool, inner = sm.ravel(), np.ones_like(sm, bool)
        lo, hi = np.quantile(pool, [0.6, 0.9])
        cand = np.argwhere(inner & (sm >= lo) & (sm <= hi))
        if not len(cand):
            cand = np.argwhere(inner)
        rng = np.random.default_rng(0)
        first = cand[rng.integers(len(cand))]
        far = cand[np.hypot(*(cand - first).T) > 0.25 * max(H, W)]
        second = far[rng.integers(len(far))] if len(far) else cand[rng.integers(len(cand))]
        ch, cw = self.shapes[self.cl]
        boxes = []
        for y, x in (first, second):
            cy, cx = int(y * fac), int(x * fac)
            y0 = int(np.clip(cy - self.cpx // 2, 0, max(0, ch - self.cpx)))
            x0 = int(np.clip(cx - self.cpx // 2, 0, max(0, cw - self.cpx)))
            boxes.append((x0, y0, x0 + self.cpx, y0 + self.cpx))
        return tissue, boxes

    def _crops(self, name, boxes=None, level=None):
        from plexora.plugins.qc.server import plane as qc_plane

        level = self.cl if level is None else level
        with qc_plane.reader(self.image_data) as src:
            return [self._plane(src, name, level, box) for box in (boxes or self.boxes)]

    def _range(self, ov, crops, q):
        pool = np.concatenate([ov[self.tissue].ravel()] + [c.ravel() for c in crops])
        lo, hi = np.quantile(pool, q)
        return float(lo), float(hi)

    def _cells(self):
        frame = self.ds.table.geometry()
        schema = self.ds.schema
        x = frame[schema.x].to_numpy().astype(np.float64)
        y = frame[schema.y].to_numpy().astype(np.float64)
        ok = np.isfinite(x) & np.isfinite(y)
        self.xy, self.ok = np.c_[x, y], ok
        self.markers = list(self.ds.table.markers)

    def values(self, name):
        """The cells' table values for a channel, or None (no table, not measured)."""
        from plexora.plugins.qc.server import class_rules

        if self.ds is None:
            return None
        column = class_rules.table_column(name, self.markers)
        if column is None:
            return None
        try:
            v = np.asarray(self.ds.table.columns([column])[column], dtype=np.float64)
        except Exception:  # noqa: BLE001
            return None
        return np.where(self.ok & np.isfinite(v), v, np.nan)


_CACHE: dict = {}


def image_context(session, project) -> _Image:
    """The shared part of one image's pictures, cached by the image and table
    identity (a new mask or table builds it again)."""
    from plexora.plugins.qc.server.cells import inputs as cell_inputs

    try:
        stamp = cell_inputs.table_fingerprint(session.project(project))
    except Exception:  # noqa: BLE001
        stamp = None
    key = (project, str(session.image_data(project)), stamp)
    held = _CACHE.get(key)
    if held is None:
        _CACHE.clear()
        held = _CACHE[key] = _Image(session, project)
    return held


def _norm(a, lo, hi):
    return np.clip((np.asarray(a, np.float32) - lo) / max(hi - lo, 1e-6), 0, 1)


def _rgb(marker, nuc):
    return (np.stack([marker, marker, np.clip(0.45 * nuc, 0, 1)], -1) * 255).astype(np.uint8)


def _fit(array, width, height=None):
    from PIL import Image

    im = Image.fromarray(array)
    s = width / im.width if height is None else min(width / im.width, height / im.height)
    size = (max(1, round(im.width * s)), max(1, round(im.height * s)))
    return im.resize(size, Image.BILINEAR), s


def _cell_box(ctx, name, plane_ov):
    """D's box at level 0: round one cell of the marker's top 0.5 % (its top
    0.05 % left out), seeded; from the overview pixels when the table does
    not measure the channel."""
    side = ctx.cells0
    v = ctx.values(name)
    if v is not None and np.isfinite(v).sum() > 200:
        finite = np.isfinite(v)
        q1, q2 = np.nanquantile(v, [0.995, 0.9995])
        band = np.flatnonzero(finite & (v >= q1) & (v <= q2))
        if not len(band):
            band = np.flatnonzero(finite & (v >= q1))
        i = band[np.random.default_rng(0).integers(len(band))]
        cx, cy = ctx.xy[i]
    else:
        from scipy import ndimage

        sm = ndimage.uniform_filter(plane_ov, 3)
        pool = np.where(ctx.tissue, sm, np.nan)
        t = np.nanquantile(pool, 0.995)
        yy, xx = np.unravel_index(int(np.nanargmin(np.abs(pool - t))), pool.shape)
        f = ctx.factor(ctx.ov)
        cx, cy = xx * f, yy * f
    x0 = int(np.clip(cx - side / 2, 0, max(0, ctx.w0 - side)))
    y0 = int(np.clip(cy - side / 2, 0, max(0, ctx.h0 - side)))
    return x0, y0, x0 + side, y0 + side


def _tissue_range(ctx, plane_ov, q=(0.005, 0.998)):
    """A channel's display range over the tissue of the overview alone."""
    pool = plane_ov[ctx.tissue] if ctx.tissue.any() else plane_ov.ravel()
    lo, hi = np.quantile(pool, q)
    return float(lo), float(max(hi, lo + 1e-6))


def _partner_box(ctx, partner_name):
    """Row E's field (at the crop level): the field-sized grid square
    holding the most of the partner's top-decile cells, off the image's
    border; None without a table that measures the partner."""
    v = ctx.values(partner_name)
    if v is None or np.isfinite(v).sum() < 200:
        return None
    hi = np.isfinite(v) & (v >= np.nanquantile(v, 0.9))
    xy = ctx.xy[hi]
    side = ctx.field0
    gx, gy = (xy[:, 0] // side).astype(int), (xy[:, 1] // side).astype(int)
    ok = (gx >= 1) & (gy >= 1) & ((gx + 2) * side <= ctx.w0) & ((gy + 2) * side <= ctx.h0)
    if not ok.any():
        ok = np.ones_like(gx, bool)
    cells, counts = np.unique(np.c_[gx[ok], gy[ok]], axis=0, return_counts=True)
    bx, by = cells[int(np.argmax(counts))]
    f = ctx.factor(ctx.cl)
    ch, cw = ctx.shapes[ctx.cl]
    x0 = int(min(bx * side / f, max(0, cw - ctx.cpx)))
    y0 = int(min(by * side / f, max(0, ch - ctx.cpx)))
    return x0, y0, x0 + ctx.cpx, y0 + ctx.cpx


def render(session, project, channel, *, partner_name=None, partner_words=None,
           fmt="png", store=True) -> dict:
    """{image, format, size, manifest, artifact}: one channel's picture."""
    from PIL import Image, ImageDraw

    from plexora.agent import artifacts
    from plexora.plugins.qc.server import plane as qc_plane

    ctx = image_context(session, project)
    if channel not in ctx.index or ctx.index[channel] is None:
        raise ValueError(f"{channel} is not a channel of this image")
    panel, cap, top_w = STAIN["panel_px"], 16, STAIN["overview_px"]
    with qc_plane.reader(ctx.image_data) as src:
        ov = ctx._plane(src, channel, ctx.ov)
        crops = [ctx._plane(src, channel, ctx.cl, box) for box in ctx.boxes]
        dbox = _cell_box(ctx, channel, ov)
        dm = ctx._plane(src, channel, 0, dbox)
        dn = ctx._plane(src, ctx.nuclear, 0, dbox)
        ebox = _partner_box(ctx, partner_name) if partner_name else None
        if ebox is not None:
            em = ctx._plane(src, channel, ctx.cl, ebox)
            ep = ctx._plane(src, partner_name, ctx.cl, ebox)
            en = ctx._plane(src, ctx.nuclear, ctx.cl, ebox)
            p_ov = ctx._plane(src, partner_name, ctx.ov)
    mlo, mhi = ctx._range(ov, crops, (0.005, 0.998))
    a_img, sa = _fit(_rgb(_norm(ov, mlo, mhi), _norm(ctx.dna_ov, ctx.nlo, ctx.nhi)),
                     top_w, STAIN["overview_max_h"])
    draw = ImageDraw.Draw(a_img)
    s_cl = sa * ctx.factor(ctx.cl) / ctx.factor(ctx.ov)
    for (x0, y0, x1, y1), label in zip(ctx.boxes, "BC"):
        draw.rectangle([x0 * s_cl, y0 * s_cl, x1 * s_cl, y1 * s_cl], outline=(255, 255, 255),
                       width=2)
        draw.text((x0 * s_cl + 3, y0 * s_cl + 2), label, fill=(255, 255, 255))
    s0 = sa / ctx.factor(ctx.ov)
    dx, dy = (dbox[0] + dbox[2]) / 2 * s0, (dbox[1] + dbox[3]) / 2 * s0
    draw.ellipse([dx - 7, dy - 7, dx + 7, dy + 7], outline=(0, 255, 255), width=2)
    draw.text((dx + 9, dy - 6), "D", fill=(0, 255, 255))
    tiles = [_fit(_rgb(_norm(c, mlo, mhi), _norm(n, ctx.nlo, ctx.nhi)), panel)[0]
             for c, n in zip(crops, ctx.dna_crops)]
    d_top = max(mhi, float(np.quantile(dm, 0.995)))
    d_tile = _fit(_rgb(_norm(dm, mlo, d_top), _norm(dn, ctx.nlo, ctx.nhi)), panel)[0]
    tiles.append(d_tile.resize((panel, panel), Image.NEAREST if dm.shape[0] < panel / 2
                               else Image.BILINEAR))
    field_um = ctx.field0 * ctx.pixel_um if ctx.pixel_um else None
    cells_um = ctx.cells0 * ctx.pixel_um if ctx.pixel_um else None
    rows = 1
    e_tiles = []
    if ebox is not None:
        rows = 2
        # Row E's contrast is each channel's range over the whole tissue: the
        # field it was drawn in may hold a blob of debris that, pooled in,
        # draws the rest of the field black (lsp35766_p193_2 E-cadherin: the
        # top went from 823 to 5882).
        elo, ehi = _tissue_range(ctx, ov)
        plo, phi = _tissue_range(ctx, p_ov)
        M, P, N = _norm(em, elo, ehi), _norm(ep, plo, phi), _norm(en, ctx.nlo, ctx.nhi)
        merge_m = _norm(em, max(elo, float(np.median(em))), ehi)
        for arr in (np.stack([M, M, 0.45 * N], -1), np.stack([P, 0 * P, np.clip(P + 0.45 * N, 0, 1)], -1),
                    np.stack([P, merge_m, P], -1)):
            e_tiles.append(_fit((np.clip(arr, 0, 1) * 255).astype(np.uint8), panel)[0]
                           .resize((panel, panel), Image.BILINEAR))
    width = max(a_img.width, 3 * panel + 8)
    height = cap + a_img.height + rows * (2 * cap + panel + 4)
    canvas = Image.new("RGB", (width, height), (20, 20, 20))
    dc = ImageDraw.Draw(canvas)
    dc.text((4, 2), f"A  whole tissue  ({channel}, {project})", fill=(230, 230, 230))
    canvas.paste(a_img, (0, cap))
    y = cap + a_img.height
    um = (lambda v: f"{v:.0f} um" if v else "")
    caps = [f"B  field {um(field_um)}", f"C  field {um(field_um)}",
            f"D  a few cells {um(cells_um)} (circle in A)"]
    for i, tile in enumerate(tiles):
        dc.text((i * (panel + 4) + 4, y + 2), caps[i], fill=(230, 230, 230))
        canvas.paste(tile, (i * (panel + 4), y + cap))
    if e_tiles:
        y += cap + panel + 4
        dc.text((4, y + 2), f"E  co-expression in a field dense in {partner_name}-high cells "
                            f"({um(field_um)}); {partner_words or ''}", fill=(230, 230, 230))
        e_caps = [f"E1  {channel} alone", f"E2  {partner_name} alone",
                  f"E3  merge: {channel} green, {partner_name} magenta (both = white)"]
        for i, tile in enumerate(e_tiles):
            dc.text((i * (panel + 4) + 4, y + cap + 2), e_caps[i], fill=(230, 230, 230))
            canvas.paste(tile, (i * (panel + 4), y + 2 * cap))
    manifest = {"schema_version": 1, "kind": KIND, "renderer": RENDERER, "project": project,
                "channel": channel, "size": [canvas.width, canvas.height],
                "field_um": round(field_um, 1) if field_um else None,
                "cells_um": round(cells_um, 1) if cells_um else None,
                "levels": {"overview": ctx.ov, "fields": ctx.cl},
                "partner": partner_name if ebox is not None else None,
                "partner_words": partner_words if ebox is not None else None,
                "contrast": [round(mlo, 4), round(mhi, 4)]}
    artifact = None
    if store:
        from plexora.server.utils import fast_png

        artifact = artifacts.put(project, fast_png.encode_rgb8_png(np.asarray(canvas)),
                                 manifest, kind=KIND)
    buf = io.BytesIO()
    if fmt == "webp":
        canvas.save(buf, "WEBP", quality=88)
    else:
        fmt = "png"
        canvas.save(buf, "PNG")
    return {"image": buf.getvalue(), "format": fmt, "size": (canvas.width, canvas.height),
            "manifest": manifest, "artifact": artifact}


# -- the question --------------------------------------------------------------------------

QUESTION = (
    "Is {marker} a real stain? Before you weigh the picture, write how a working stain of "
    "{marker} should look (`expectation`): its compartment (membrane ring, nuclear, "
    "cytoplasmic, punctate granules, fibres), which cells, how common, and how they would sit "
    "in this tissue (lining vessels, clustered in lymphoid follicles or aggregates, "
    "epithelium, scattered single cells). Then look at every scale. The main question: is "
    "there clear NON-RANDOM BIOLOGICAL STRUCTURE -- a distinct subset of cells stained where "
    "the stain belongs, arranged as the expectation says (clustering, lining, networks, a "
    "compartment of the tissue)? It may be rare or dim: a few dozen convincing cells in the "
    "right place is a real stain. Signs of failure: haze that follows the tissue outline or "
    "cell density rather than a cell type; every cell lit about alike; uniform grain; blobs, "
    "debris or speckle not tied to cells; glow along the edge or in folds; stripes or tiles; "
    "the tissue's autofluorescence pattern. The brightest spots are often artefacts: do not "
    "let them carry the verdict. In row E, a working marker's cells sit on (a subset of) the "
    "partner's cells as the relation says (white or green cells beside magenta ones); a "
    "failed one lights partner-negative tissue as much, or follows texture. If the partner "
    "itself shows no cellular structure, row E tells you nothing. `real`: a clear specific "
    "population; `weak`: a real one at low contrast; `failed`: none; `cannot_tell`.")

READING = ("the stain-check picture: A whole tissue (marker yellow, nuclei dim blue; white "
           "boxes B and C, cyan circle D); B and C two tissue-scale fields of typical cell "
           "density, the same two places for every channel of the image (chosen by the "
           "nuclear stain, never by the marker), so an empty B and C is no evidence of "
           "failure for a rare population; D a few cells round one of the marker's brightest "
           "cells (its extreme top left out); row E, when there, the marker with a partner "
           "from the marker graph in a field dense in the partner's cells. Contrast is per "
           "channel, so a failed stain shows as haze, grain or debris, not as black.")
