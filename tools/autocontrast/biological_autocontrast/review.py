"""A self-contained HTML report of proposals: the overview at full scale and
under each window (the same linear LUT as frag.glsl `range_clamp`), the
histogram with every window marked in native units, clipping, and the sampled
patches. Review and editing happen in Plexora; this only shows.
"""

from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path

import numpy as np

from biological_autocontrast.domain import to_norm
from biological_autocontrast.sampling import CATEGORIES

CATEGORY_COLOURS = {"high": (239, 83, 80), "intermediate": (255, 179, 0),
                    "background": (66, 165, 245), "texture": (171, 71, 188),
                    "random": (102, 187, 106)}
WINDOW_COLOURS = {"proposal": "#e8590c", "label": "#2f9e44", "percentile": "#1c7ed6",
                  "gmm": "#ae3ec9", "fixed": "#868e96"}
THUMB = 360


def lut(norm, low, high):
    """frag.glsl range_clamp: t = clamp((v - lo) / (hi - lo), 0, 1)."""
    return np.clip((norm - low) / max(high - low, 1e-12), 0.0, 1.0)


def _png(array01, mode="L", fmt="PNG"):
    from PIL import Image

    data = (np.clip(array01, 0, 1) * 255 + 0.5).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(data, mode=mode).save(buffer, format=fmt, **({"quality": 88} if fmt == "JPEG" else {}))
    return buffer.getvalue()


def _recode(png_bytes):
    """A PNG as JPEG, for embedding (the PNG on disk stays lossless)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.open(io.BytesIO(png_bytes)).convert("RGB").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _uri(data, kind="png"):
    return f"data:image/{kind};base64," + base64.b64encode(data).decode()


def _thumb(norm, size=THUMB):
    step = max(1, int(np.ceil(max(norm.shape) / size)))
    if step == 1:
        return norm
    h, w = (norm.shape[0] // step) * step, (norm.shape[1] // step) * step
    if h == 0 or w == 0:
        return norm[::step, ::step]
    return norm[:h, :w].reshape(h // step, step, w // step, step).mean(axis=(1, 3))


def _boxes_image(thumb01, selection, scale):
    from PIL import Image, ImageDraw

    base = (np.clip(thumb01, 0, 1) * 255).astype(np.uint8)
    rgb = Image.fromarray(np.stack([base] * 3, axis=-1), mode="RGB")
    draw = ImageDraw.Draw(rgb)
    half = selection.footprint / 2 / scale
    for patch in selection.patches:
        cx, cy = patch["cx"] / scale, patch["cy"] / scale
        draw.rectangle([cx - half, cy - half, cx + half, cy + half],
                       outline=CATEGORY_COLOURS[patch["category"]], width=2)
    buffer = io.BytesIO()
    rgb.save(buffer, format="PNG")
    return buffer.getvalue()


def contact_sheet(patches, selection, low, high, cols=6, tile=96):
    """PNG bytes: the patches under [low, high] (canonical), bordered by category."""
    from PIL import Image

    n = len(patches)
    if n == 0:
        return None
    rows = int(np.ceil(n / cols))
    sheet = Image.new("RGB", (cols * (tile + 4), rows * (tile + 4)), (20, 20, 20))
    for i in range(n):
        img = (lut(patches[i], low, high) * 255).astype(np.uint8)
        tile_img = Image.fromarray(img, mode="L").resize((tile, tile)).convert("RGB")
        frame = Image.new("RGB", (tile + 4, tile + 4), CATEGORY_COLOURS[selection.patches[i]["category"]])
        frame.paste(tile_img, (2, 2))
        sheet.paste(frame, ((i % cols) * (tile + 4), (i // cols) * (tile + 4)))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue()


def _histogram_svg(pixels_raw, windows, offset, full_scale, width=420, height=130):
    data = np.asarray(pixels_raw, dtype=np.float64)
    data = data[np.isfinite(data)]
    floor = max(offset, 0) + 1.0
    lo, hi = np.log10(floor), np.log10(max(full_scale, floor * 10))
    edges = np.linspace(lo, hi, 121)
    counts, _ = np.histogram(np.log10(np.maximum(data, floor)), bins=edges)
    counts = np.log1p(counts)
    top = counts.max() or 1
    pad = 4
    def x_of(value):
        v = np.log10(max(float(value), floor))
        return pad + (v - lo) / (hi - lo) * (width - 2 * pad)
    bar = (width - 2 * pad) / len(counts)
    parts = [f'<svg viewBox="0 0 {width} {height + 16}" class="hist" role="img">']
    for i, c in enumerate(counts):
        h = c / top * (height - 10)
        parts.append(f'<rect x="{pad + i * bar:.1f}" y="{height - h:.1f}" width="{bar + 0.2:.1f}" '
                     f'height="{h:.1f}" class="bar"/>')
    for name, window in windows.items():
        if not window:
            continue
        colour = WINDOW_COLOURS.get(name, "#888")
        dash = "" if name in ("proposal", "label") else ' stroke-dasharray="3 3"'
        for value in window:
            parts.append(f'<line x1="{x_of(value):.1f}" x2="{x_of(value):.1f}" y1="0" y2="{height}" '
                         f'stroke="{colour}" stroke-width="2"{dash}><title>{name}: {value:g}</title></line>')
    for tick in range(int(np.floor(lo)), int(np.ceil(hi)) + 1):
        x = x_of(10 ** tick)
        if pad <= x <= width - pad:
            parts.append(f'<text x="{x:.1f}" y="{height + 13}" class="tick">1e{tick}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _fmt(v):
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.4g}"
    return html.escape(str(v))


def channel_card(row, features, label=None, out_dir=None) -> str:
    dom = features.domain
    norm = features.overview_norm
    thumb = _thumb(norm)
    scale = norm.shape[1] / max(1, thumb.shape[1])
    windows = {"proposal": [row["display_min_raw"], row["display_max_raw"]]}
    if label:
        windows["label"] = list(label)
    for name, window in (row.get("baseline_windows") or {}).items():
        windows[name] = window
    views = {"full scale": _uri(_png(lut(thumb, 0.0, 1.0), fmt="JPEG"), "jpeg")}
    for name, window in windows.items():
        if window:
            views[name] = _uri(_png(lut(thumb, to_norm(window[0], dom), to_norm(window[1], dom)),
                                    fmt="JPEG"), "jpeg")
    tissue = features.pixels
    lo_n, hi_n = row["display_min_norm"], row["display_max_norm"]
    clip_white = float((tissue >= hi_n).mean()) if tissue.size else 0.0
    clip_black = float((tissue <= lo_n).mean()) if tissue.size else 0.0
    pixels_raw = dom.offset + tissue.astype(np.float64) * dom.span
    hist = _histogram_svg(pixels_raw, windows, dom.offset, dom.full_scale)
    cid = f"c{row['channel_index']}"
    buttons = "".join(
        f'<button type="button" data-src="{src}" class="{"on" if name == "proposal" else ""}">'
        f'{html.escape(name)}</button>' for name, src in views.items())
    sheets = ""
    if features.patches is not None and features.selection and features.selection.patches:
        raw_sheet = contact_sheet(features.patches, features.selection, 0.0, 1.0)
        win_sheet = contact_sheet(features.patches, features.selection, lo_n, hi_n)
        boxes = _boxes_image(lut(thumb, lo_n, hi_n), features.selection, scale)
        if out_dir:
            folder = Path(out_dir) / "contact_sheets"
            folder.mkdir(parents=True, exist_ok=True)
            stem = f"{row['channel_index']:03d}_{_safe(row['channel_name'])}"
            (folder / f"{stem}.windowed.png").write_bytes(win_sheet)
            (folder / f"{stem}.raw.png").write_bytes(raw_sheet)
            (folder / f"{stem}.locations.png").write_bytes(boxes)
        legend = " ".join(f'<span class="cat" style="border-color:rgb{CATEGORY_COLOURS[c]}">{c}</span>'
                          for c in CATEGORIES)
        fallbacks = row.get("quota_fallbacks") or {}
        sheets = (f'<details><summary>{row["n_patches"]} patches at level {row["patch_level"]}'
                  f'{" · fallbacks " + html.escape(json.dumps(fallbacks)) if fallbacks else ""}</summary>'
                  f'<div class="legend">{legend}</div><div class="sheets">'
                  f'<figure><img src="{_uri(boxes)}" alt="patch locations"><figcaption>locations</figcaption></figure>'
                  f'<figure><img src="{_uri(_recode(win_sheet), "jpeg")}" alt="windowed patches"><figcaption>proposed window</figcaption></figure>'
                  f'<figure><img src="{_uri(_recode(raw_sheet), "jpeg")}" alt="raw patches"><figcaption>full scale</figcaption></figure>'
                  f'</div></details>')
    status = (f'<span class="badge warn">abstain: {html.escape(str(row["abstain_reason"]))}</span>'
              if row["abstain"] else "")
    warns = "".join(f"<li>{html.escape(w)}</li>" for w in row.get("warnings") or [])
    label_txt = (f' · label <b>{label[0]:g} – {label[1]:g}</b>' if label else "")
    return f"""
<section class="card" id="{cid}">
  <header><h2>{row['channel_index']} · {html.escape(row['channel_name'])}</h2>{status}
    <span class="meta">{html.escape(row['method'])}{' · confidence ' + _fmt(row['confidence']) if row['confidence'] is not None else ''}</span></header>
  <div class="body">
    <div class="view"><img class="main" src="{views.get('proposal')}" alt="{html.escape(row['channel_name'])}">
      <div class="toggles" data-for="{cid}">{buttons}</div></div>
    <div class="facts">
      <p>window <b>{row['display_min_raw']:.6g} – {row['display_max_raw']:.6g}</b>
         <span class="dim">({row['display_min_norm']:.4f} – {row['display_max_norm']:.4f} of full scale)</span>{label_txt}</p>
      <p class="dim">{html.escape(dom.storage_dtype)} · {dom.effective_bits or '–'} bits ({html.escape(dom.bits_source)})
         · range {dom.offset:g}..{dom.full_scale:g} · sampled max {_fmt(row.get('raw_max_sampled'))}</p>
      <p class="dim">tissue clipped white {clip_white:.2%} · black {clip_black:.2%} · saturated {dom.saturated_fraction:.3%}</p>
      {hist}
      <p class="keys">{''.join(f'<span style="color:{WINDOW_COLOURS.get(k, "#888")}">■ {html.escape(k)}</span> ' for k in windows if windows[k])}</p>
      {f'<ul class="warns">{warns}</ul>' if warns else ''}
    </div>
  </div>
  {sheets}
</section>"""


def _safe(name):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name))


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1f;--dim:#6b6b70;--line:#e3e3e0;--warn:#c92a2a;--bar:#adb5bd}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1d1d20;--ink:#ececf0;--dim:#9a9aa2;--line:#2c2c31;--bar:#5c636a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0}
a{color:inherit;text-decoration-color:var(--line)}a:hover{text-decoration-color:currentColor}
.dim,.meta{color:var(--dim)}.note{color:var(--dim);margin:0 0 16px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:12px 0 24px;background:var(--card)}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;white-space:nowrap}th{position:sticky;top:0;background:var(--card)}
td.num{text-align:right;font-variant-numeric:tabular-nums}.tablewrap{overflow-x:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin:0 0 14px}
.card header{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:10px}
.body{display:grid;grid-template-columns:minmax(0,360px) minmax(0,1fr);gap:16px}
@media (max-width:760px){.body{grid-template-columns:1fr}}
img.main{width:100%;image-rendering:pixelated;background:#000;border-radius:4px}
.toggles{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.toggles button{font:inherit;font-size:12px;border:1px solid var(--line);background:transparent;color:var(--ink);border-radius:4px;padding:2px 8px;cursor:pointer}
.toggles button.on{background:var(--ink);color:var(--card)}
.facts p{margin:0 0 6px}.hist{width:100%;max-width:440px;height:auto}.hist .bar{fill:var(--bar)}.hist .tick{font-size:9px;fill:var(--dim)}
.badge.warn{color:var(--warn);font-weight:600;font-size:12px}.warns{margin:6px 0 0;padding-left:18px;color:var(--warn);font-size:12px}
.keys span{margin-right:8px;font-size:12px}details{margin-top:10px}summary{cursor:pointer;color:var(--dim)}
.sheets{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px}.sheets figure{margin:0}.sheets img{max-width:100%;border-radius:4px}
.sheets figcaption{font-size:12px;color:var(--dim)}.legend{margin-top:6px}.cat{border-left:10px solid;padding-left:4px;margin-right:10px;font-size:12px}
"""

JS = """
document.querySelectorAll('.toggles').forEach(function(bar){
  var img=document.querySelector('#'+bar.dataset.for+' img.main');
  bar.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
    img.src=b.dataset.src;bar.querySelectorAll('button').forEach(function(x){x.classList.toggle('on',x===b)});});
});
"""


def write_review(rows, features_by_channel, out_dir, labels=None, title=None) -> Path:
    """overview.html: a summary table, then one card per channel. `labels` is
    {channel_index: (min_raw, max_raw)} to show beside the proposal."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = labels or {}
    heuristic = not any(r.get("model_available") for r in rows)
    head = rows[0] if rows else {}
    columns = ("channel", "method", "conf", "abstain", "min raw", "max raw", "min norm",
               "max norm", "label", "percentile", "gmm", "bits")
    body = []
    for r in rows:
        label = labels.get(r["channel_index"])
        b = r.get("baseline_windows") or {}
        def w(x):
            return f"{x[0]:.5g}–{x[1]:.5g}" if x else "–"
        body.append(
            f'<tr><td><a href="#c{r["channel_index"]}">{r["channel_index"]} {html.escape(r["channel_name"])}</a></td>'
            f'<td>{html.escape(r["method"])}</td><td class="num">{_fmt(r["confidence"])}</td>'
            f'<td>{html.escape(r["abstain_reason"] or "")}</td>'
            f'<td class="num">{r["display_min_raw"]:.5g}</td><td class="num">{r["display_max_raw"]:.5g}</td>'
            f'<td class="num">{r["display_min_norm"]:.4f}</td><td class="num">{r["display_max_norm"]:.4f}</td>'
            f'<td class="num">{w(label)}</td><td class="num">{w(b.get("percentile"))}</td>'
            f'<td class="num">{w(b.get("gmm"))}</td><td>{r.get("effective_bits") or "–"} {html.escape(r.get("bits_source") or "")}</td></tr>')
    cards = [channel_card(r, features_by_channel[r["channel_index"]], labels.get(r["channel_index"]), out_dir)
             for r in rows if r["channel_index"] in features_by_channel]
    note = ("Heuristic proposals (Plexora's percentile calibration): no model was given, "
            "so these are not learned windows." if heuristic else
            f"Model {html.escape(str(head.get('model_version')))} · checkpoint "
            f"{html.escape(str(head.get('checkpoint_id')))}"
            + ("" if head.get("model_valid") else " · <b>not validated</b> (fewer than 5 labelled specimens)"))
    title = title or f"Contrast proposals · {head.get('image_id', '')}"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>{CSS}</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="note">{note}<br>{html.escape(str(head.get('image_path', '')))} · z={head.get('z')} t={head.get('t')} ·
overview level {head.get('overview_level')} · tissue {head.get('tissue_fraction', 0):.1%} ({html.escape(str(head.get('tissue_source')))})</p>
<div class="tablewrap"><table><thead><tr>{''.join(f'<th>{c}</th>' for c in columns)}</tr></thead>
<tbody>{''.join(body)}</tbody></table></div>
{''.join(cards)}
</main><script>{JS}</script></body></html>"""
    path = out_dir / "overview.html"
    path.write_text(page, encoding="utf-8")
    return path
