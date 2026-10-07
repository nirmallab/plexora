"""DNA retention across cycles: until which cycle can a cell be analysed?

A cyclic image re-stains the DNA in every cycle. Where a cell's nucleus has
gone by cycle k -- the tissue lifted, the cell washed away -- every marker
of cycle k and later reads glass for it. This measures, once per cell and
per cycle, the mean DNA under the cell's mask label, normalised to that DNA
channel's own window (`J = (mean - lo) / (hi - lo)`, lo the glass's median,
hi the tissue's 99.7th percentile -- `registration._window`'s rule), and
derives (`derive_retention`, pure):

- `has_nucleus`: the reference cycle's J is at least `present_floor`;
- `retained` per cycle: J at least `retained_ratio` of the reference's;
- `last_good_cycle`: the last cycle before the first one not retained
  (monotone: a nucleus lost is lost);
- per cycle the share of nucleated cells retained there, and through it;
  `reliable_through_cycle`: the last cycle through which at least
  `reliable_fraction` of the nucleated cells keep their nucleus.

And `no_nucleus`: a label whose reference DNA is no brighter than the
glass's noise (`no_nucleus_k` robust spreads over the glass median, or the
presence floor, whichever is higher). `digest_line` says it in one line.

Only the DNA channels are read (one per cycle, not every marker), at the
coarsest pyramid level no coarser than `max_um_per_px` where a label is
still `min_nucleus_px` across; the mask provider is opened ONCE
(`render._mask_for`), never `SegHandle.read_region` per tile. Stored like
Segmentation QC's (`<QC store>/dna/<fp>.parquet|.json`), fingerprinted by
the image, the mask, the DNA channels, the reference and the parameters.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from time import perf_counter

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import schemas

VERSION = "1"
TILE_PX = 2048
#: Labels beyond this are refused (too_large), as Segmentation QC does.
MAX_LABELS = 60_000_000

_GUARD = threading.Lock()
_MEMORY: dict = {}


# -- the derivation (pure) ---------------------------------------------------------------


def derive_retention(J, ref, order, params=None) -> dict:
    """Per cell and per cycle from `J` (cells x cycles, the columns in the
    cycle order `order`, `ref` the reference's column): {has_nucleus,
    retained (cells x cycles), last_good_cycle (0 for a cell with no
    nucleus), cycles [{index, fraction_retained, cumulative_retained,
    n_lost}], reliable_through_cycle (None when even the first cycle falls
    short), n_nucleated}."""
    params = {**schemas.DNA_RETENTION, **(params or {})}
    J = np.asarray(J, dtype=np.float64)
    order = [int(o) for o in order]
    n, k = J.shape if J.ndim == 2 else (0, len(order))
    reference = J[:, ref] if n else np.zeros(0)
    has_nucleus = np.isfinite(reference) & (reference >= params["present_floor"])
    with np.errstate(invalid="ignore"):
        retained = J >= params["retained_ratio"] * reference[:, None]
    retained &= np.isfinite(J)
    retained[:, ref] = has_nucleus
    retained &= has_nucleus[:, None]
    # The first cycle (in order) not retained; k when every one is.
    bad = ~retained
    first_bad = np.where(bad.any(axis=1), bad.argmax(axis=1), k) if n else np.zeros(0, int)
    order_arr = np.asarray(order, dtype=np.int64)
    last_good = np.where(first_bad > 0, order_arr[np.maximum(first_bad - 1, 0)], 0) \
        if n and k else np.zeros(n, dtype=np.int64)
    last_good = np.where(has_nucleus, last_good, 0).astype(np.int64)
    nucleated = int(has_nucleus.sum())
    cycles = []
    reliable = None
    for position, index in enumerate(order):
        if nucleated:
            here = float(retained[has_nucleus, position].mean())
            through = float((first_bad[has_nucleus] > position).mean())
            lost = int(((first_bad == position) & has_nucleus).sum())
        else:
            here = through = 0.0
            lost = 0
        cycles.append({"index": index, "fraction_retained": round(here, 4),
                       "cumulative_retained": round(through, 4), "n_lost": lost})
        if nucleated and through >= params["reliable_fraction"] and (
                reliable is not None or position == 0):
            reliable = index
    return {"has_nucleus": has_nucleus, "retained": retained, "last_good_cycle": last_good,
            "cycles": cycles, "reliable_through_cycle": reliable, "n_nucleated": nucleated}


def no_nucleus_at(glass, lo, hi, params=None) -> float:
    """The J under which a label has no nucleus: `no_nucleus_k` robust
    spreads (1.4826 MAD) of the glass over its median, in the window's
    units, or the presence floor when that is higher."""
    params = {**schemas.DNA_RETENTION, **(params or {})}
    glass = np.asarray(glass, dtype=np.float64)
    glass = glass[np.isfinite(glass)]
    spread = 1.4826 * float(np.median(np.abs(glass - np.median(glass)))) if glass.size else 0.0
    noise = params["no_nucleus_k"] * spread / max(float(hi) - float(lo), 1e-9)
    return float(max(noise, params["present_floor"]))


def digest_line(summary) -> str | None:
    """"reliable through cycle 6 of 10 (>=95% of nucleated cells keep their
    nucleus); 8.2% lose it by cycle 10; 1.3% of labels have no nucleus"."""
    if not summary or not summary.get("cycles"):
        return None
    cycles = summary["cycles"]
    params = summary.get("params") or schemas.DNA_RETENTION
    share = f"{params.get('reliable_fraction', 0.95):.0%}"
    last = cycles[-1]
    through = summary.get("reliable_through_cycle")
    position = next((i for i, c in enumerate(cycles) if c["index"] == through), None)
    head = (f"reliable through cycle {position + 1} of {len(cycles)}"
            if position is not None else f"not reliable even in the first of {len(cycles)} "
                                         "cycles")
    lost = 100.0 * (1.0 - float(last.get("cumulative_retained") or 0.0))
    n_cells = int(summary.get("n_cells") or 0)
    no_nucleus = 100.0 * int(summary.get("n_no_nucleus") or 0) / n_cells if n_cells else 0.0
    return (f"{head} (>={share} of nucleated cells keep their nucleus); {lost:.1f}% lose it "
            f"by cycle {len(cycles)}; {no_nucleus:.1f}% of labels have no nucleus")


# -- where it lives -----------------------------------------------------------------------


def _folder(project):
    from plexora import api

    path = api.store(project, "qc").directory() / "dna"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path, data: bytes):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# -- inputs --------------------------------------------------------------------------------


def plan(session, project, *, reference=None, params=None) -> tuple:
    """(fingerprint, context) of a run, without reading a pixel. Needs a
    mask and at least two cycles with a DNA channel each
    (`precondition_missing` otherwise)."""
    from plexora.agent.render import resolve_channel
    from plexora.plugins.qc.server import cycles as cycle_rules
    from plexora.plugins.qc.server import results
    from plexora.plugins.qc.server.segqc import run as segqc
    from plexora.server.utils import pixel_scale, source_image

    record = session.project(project)
    if record.image.is_blank:
        raise AgentError("unsupported_modality", "this sample has no image to check")
    seg = record.segmentation
    if not seg.available:
        raise AgentError("precondition_missing",
                         "the segmentation mask is still being prepared" if seg.pending
                         else "DNA retention needs a segmentation mask: add one first",
                         detail={"requires": ["segmentation"]})
    names = segqc.channel_names(record)
    override = results.load(project).get("cycles_override")
    found = cycle_rules.infer(names, override=override)
    dna = [{"index": int(c["index"]), "channel": c["nuclear"],
            "markers": list(c.get("channels") or [])}
           for c in found.get("cycles") or [] if c.get("nuclear")]
    if len(dna) < 2:
        raise AgentError("precondition_missing",
                         "DNA retention needs two or more cycles with a DNA channel each "
                         f"(found {len(dna)}); set the cycles (set_qc_cycles) if the names "
                         "do not say", detail={"channels": names[:100]})
    channels = [d["channel"] for d in dna]
    chosen = reference or segqc.settings(project).get("dna_channel") \
        or segqc.detected_dna(names)
    if reference and reference not in channels:
        raise AgentError("invalid_input", f"{reference!r} is not one of the cycles' DNA "
                         "channels", detail={"dna_channels": channels})
    reference = chosen if chosen in channels else channels[0]
    channel_records = list(record.image.real_channels)
    for entry in dna:
        _i, rec = resolve_channel(entry["channel"], channel_records)
        entry["key"] = source_image.channel_key(rec)
    image_data = session.image_data(project)
    identity = source_image.ReaderShelf.identity_of(image_data)
    image_stamp = None if str(identity or "").startswith("node://") \
        else segqc._stamp(identity)
    mask = segqc.mask_identity(record)
    params = {**schemas.DNA_RETENTION, **(params or {})}
    pixel = pixel_scale.pixel_size(record)
    pixel_um = float(pixel["value"]) if pixel else None
    blob = json.dumps({"v": VERSION, "image": identity, "image_stamp": image_stamp,
                       "mask": mask, "dna": [[d["index"], d["key"]] for d in dna],
                       "reference": reference, "params": params, "pixel_um": pixel_um},
                      sort_keys=True, default=str)
    fp = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    return fp, {"record": record, "image_data": image_data, "identity": identity,
                "image_stamp": image_stamp, "mask": mask, "dna": dna,
                "reference": reference, "params": params, "pixel_um": pixel_um,
                "cycles_method": found.get("method")}


def choose_level(source, d_full, pixel_um, params):
    """The coarsest level where a label is still `min_nucleus_px` across and
    (with a pixel size) a pixel no coarser than `max_um_per_px`."""
    level = 0
    while level + 1 < max(1, source.levels):
        nxt = level + 1
        if d_full / (2 ** nxt) < params["min_nucleus_px"]:
            break
        if pixel_um and pixel_um * (2 ** nxt) > params["max_um_per_px"]:
            break
        level = nxt
    return level


def _windows(found, source, dna):
    """{channel: (lo, hi, glass sample)}: the glass's median (outside the
    feathered tissue `found`, `tissue.for_project`) and the tissue's 99.7th
    percentile, from each DNA channel's overview."""
    from plexora.agent.evidence import image_qc
    from plexora.plugins.qc.server import tissue as tissue_rules

    level = image_qc.overview_level(source)
    out = {}
    for entry in dna:
        plane, _level, _ceiling = image_qc.read_overview(source, entry["key"], level)
        tight = tissue_rules.to_shape(found["mask"], plane.shape)
        glass = ~tissue_rules.to_shape(found["feathered"], plane.shape)
        on = plane[tight] if tight.any() else plane.ravel()
        off = plane[glass] if glass.sum() >= 64 else np.asarray(
            [np.percentile(plane, 1.0)], dtype=np.float32)
        lo = float(np.median(off))
        hi = float(np.percentile(on, 99.7)) if on.size else lo + 1.0
        out[entry["channel"]] = (lo, max(hi, lo + 1e-6), off)
    return out


# -- the run --------------------------------------------------------------------------------


def run(session, project, fp, context, *, progress=None, check_cancelled=None) -> dict:
    from plexora.agent import render
    from plexora.plugins.qc.server.segqc import run as segqc
    from plexora.server.utils import source_image

    def say(done, total, message):
        if progress is not None:
            progress(done=done, total=total, message=message)

    def stop_if_asked():
        if check_cancelled is not None:
            check_cancelled()

    record = context["record"]
    params = context["params"]
    dna = context["dna"]
    started = perf_counter()
    provider, status, reason, _locator = render._mask_for(record)
    if provider is None:
        raise AgentError("resource_unavailable", reason or f"the mask is {status}")
    extra = int(getattr(record.segmentation, "extra_levels", 0) or 0)
    # Read before the image is opened here: the shelf's reader is not
    # re-entrant, and the tissue may need one overview read of its own.
    from plexora.plugins.qc.server import tissue as tissue_rules

    say(0, 1, "measuring the glass and the tissue")
    found = tissue_rules.for_project(session, project)
    with source_image.SHELF.reader(context["image_data"]) as source:
        if source.is_brightfield:
            raise AgentError("unsupported_modality", "a brightfield image has no DNA cycles")
        indexes = []
        for entry in dna:
            index = source.channel_index(entry["key"])
            if index is None:
                raise AgentError("invalid_input",
                                 f"{entry['channel']!r} is not a channel of this image")
            indexes.append(index)
        height0, width0 = source.level_shape(0)
        try:
            area0, _sample = segqc.size_labels(provider, extra, height0, width0)
        except Exception as exc:
            raise AgentError("resource_unavailable", f"the mask could not be read ({exc})") \
                from exc
        if area0 is None:
            raise AgentError("precondition_missing",
                             "the mask has no complete labels in the sampled blocks: is it "
                             "empty, or not a label mask?")
        d_full = 2.0 * np.sqrt(area0 / np.pi)
        level = choose_level(source, d_full, context["pixel_um"], params)
        windows = _windows(found, source, dna)
        height, width = source.level_shape(level)
        boxes = list(segqc.tiles(height, width, TILE_PX))
        total = len(boxes) + 1
        capacity = 0
        counts = np.zeros(0, dtype=np.float64)
        sums = np.zeros((len(dna), 0), dtype=np.float64)
        for done, box in enumerate(boxes):
            stop_if_asked()
            say(done, total, "reading the DNA of every cycle under the mask")
            try:
                labels = segqc._labels(provider, level + extra, box)
            except Exception as exc:
                raise AgentError("resource_unavailable",
                                 f"the mask could not be read ({exc})") from exc
            top = int(labels.max()) if labels.size else 0
            if top == 0:
                continue
            if top + 1 > MAX_LABELS:
                raise AgentError("too_large", f"the mask has labels up to {top:,}; DNA "
                                 f"retention handles up to {MAX_LABELS:,}")
            if top + 1 > capacity:
                grown = max(top + 1, int(capacity * 1.5) + 1)
                counts = np.concatenate([counts, np.zeros(grown - capacity)])
                sums = np.concatenate([sums, np.zeros((len(dna), grown - capacity))], axis=1)
                capacity = grown
            flat = labels.ravel()
            counts[:top + 1] += np.bincount(flat, minlength=top + 1)
            for row, index in enumerate(indexes):
                plane, _clip = source.read(index, level, box)
                plane = np.asarray(plane, dtype=np.float64)
                if plane.shape != labels.shape:
                    fixed = np.zeros(labels.shape, dtype=np.float64)
                    h, w = min(plane.shape[0], labels.shape[0]), \
                        min(plane.shape[1], labels.shape[1])
                    fixed[:h, :w] = plane[:h, :w]
                    plane = fixed
                sums[row, :top + 1] += np.bincount(flat, weights=plane.ravel(),
                                                   minlength=top + 1)
    say(len(boxes), total, "deriving retention")
    if mask_changed(record, context):
        raise AgentError("conflict", "the image or the mask changed while DNA retention ran; "
                         "run it again", retryable=True)
    ids = np.flatnonzero(counts[1:] > 0) + 1 if counts.size > 1 else np.zeros(0, np.int64)
    if ids.size < params["min_cells"]:
        raise AgentError("precondition_missing", f"only {ids.size} labels under the mask "
                         f"({params['min_cells']} needed)")
    n_px = counts[ids]
    order = [d["index"] for d in dna]
    ref = next(i for i, d in enumerate(dna) if d["channel"] == context["reference"])
    J = np.empty((ids.size, len(dna)), dtype=np.float64)
    for row, entry in enumerate(dna):
        lo, hi, _off = windows[entry["channel"]]
        J[:, row] = (sums[row, ids] / n_px - lo) / (hi - lo)
    derived = derive_retention(J, ref, order, params)
    lo_ref, hi_ref, glass_ref = windows[context["reference"]]
    floor = no_nucleus_at(glass_ref, lo_ref, hi_ref, params)
    no_nucleus = (n_px > 0) & (J[:, ref] < floor)
    import polars as pl

    columns = {"cell_id": ids.astype(np.int64),
               "n_px": (n_px * float((2 ** level) ** 2)).astype(np.float64),
               "j_ref": J[:, ref].astype(np.float32),
               "has_nucleus": derived["has_nucleus"], "no_nucleus": no_nucleus,
               "last_good_cycle": derived["last_good_cycle"].astype(np.int16)}
    for row, entry in enumerate(dna):
        columns[f"j:{entry['channel']}"] = J[:, row].astype(np.float32)
        columns[f"retained:{entry['channel']}"] = derived["retained"][:, row]
    frame = pl.DataFrame(columns)
    cycles = []
    for row, (entry, stats) in enumerate(zip(dna, derived["cycles"])):
        lo, hi, _off = windows[entry["channel"]]
        cycles.append({**stats, "channel": entry["channel"], "markers": entry["markers"],
                       "window": [round(lo, 3), round(hi, 3)]})
    summary = {"version": VERSION, "fingerprint": fp, "project": project,
               "reference": context["reference"], "reference_cycle": order[ref],
               "cycles": cycles, "n_cycles": len(cycles),
               "reliable_through_cycle": derived["reliable_through_cycle"],
               "n_cells": int(ids.size), "n_nucleated": derived["n_nucleated"],
               "n_no_nucleus": int(no_nucleus.sum()), "no_nucleus_at": round(floor, 4),
               "params": params, "level": int(level), "pixel_um": context["pixel_um"],
               "mask": context["mask"], "cycles_method": context.get("cycles_method"),
               "timing": {"total_s": round(perf_counter() - started, 3),
                          "tiles": len(boxes)}}
    summary["digest"] = digest_line(summary)
    return {"summary": summary, "frame": frame}


def mask_changed(record, context) -> bool:
    from plexora.plugins.qc.server.segqc import run as segqc

    return segqc.mask_identity(record) != context["mask"] or (
        context["image_stamp"] is not None
        and segqc._stamp(context["identity"]) != context["image_stamp"])


# -- cache and store ------------------------------------------------------------------------


def store(project, fp, result):
    """Store a run (`<QC store>/dna/<fp>.parquet|.json`) as the current one;
    returns its summary."""
    import io

    from plexora.plugins.qc.server.results import now_iso

    folder = _folder(project)
    summary = {**result["summary"], "computed_at": now_iso()}
    buffer = io.BytesIO()
    result["frame"].write_parquet(buffer)
    _atomic_write(folder / f"{fp}.parquet", buffer.getvalue())
    _atomic_write(folder / f"{fp}.json", json.dumps(summary, default=str).encode("utf-8"))
    _atomic_write(folder / "current.json", json.dumps({"fingerprint": fp}).encode("utf-8"))
    _sweep(folder, keep=3)
    with _GUARD:
        _MEMORY[(project, fp)] = summary
        while len(_MEMORY) > 4:
            _MEMORY.pop(next(iter(_MEMORY)))
    return summary


def _sweep(folder, keep):
    metas = sorted((p for p in folder.glob("*.json")
                    if p.name not in ("current.json", "running.json")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in metas[keep:]:
        for path in (stale, stale.with_suffix(".parquet")):
            try:
                path.unlink()
            except OSError:
                pass


def load_summary(project, fp):
    with _GUARD:
        held = _MEMORY.get((project, fp))
    if held is not None:
        return held
    folder = _folder(project)
    summary = _read_json(folder / f"{fp}.json")
    if not summary or summary.get("version") != VERSION \
            or not (folder / f"{fp}.parquet").is_file():
        return None
    with _GUARD:
        _MEMORY[(project, fp)] = summary
    return summary


def current_fingerprint(project):
    return (_read_json(_folder(project) / "current.json") or {}).get("fingerprint")


def current(project):
    """The summary of the current result, or None."""
    fp = current_fingerprint(project)
    return load_summary(project, fp) if fp else None


def frame(project, fp=None):
    """The per-cell table of a result (default the current one), or None."""
    import polars as pl

    fp = fp or current_fingerprint(project)
    if not fp:
        return None
    try:
        return pl.read_parquet(_folder(project) / f"{fp}.parquet")
    except Exception:
        return None


def calls(project):
    """(summary, per-cell frame) of the current result, or None -- what the
    cells' calls read (`cells.calls.derive(dna=...)`)."""
    summary = current(project)
    if summary is None:
        return None
    table = frame(project, summary.get("fingerprint"))
    if table is None:
        return None
    return summary, table


def clear(project) -> bool:
    folder = _folder(project)
    had = (folder / "current.json").is_file()
    try:
        (folder / "current.json").unlink()
    except OSError:
        pass
    return had


def load_or_run(session, project, *, reference=None, params=None, force=False, progress=None,
                check_cancelled=None) -> tuple:
    """(summary, reused)."""
    fp, context = plan(session, project, reference=reference, params=params)
    if not force:
        summary = load_summary(project, fp)
        if summary is not None:
            _atomic_write(_folder(project) / "current.json",
                          json.dumps({"fingerprint": fp}).encode("utf-8"))
            return summary, True
    result = run(session, project, fp, context, progress=progress,
                 check_cancelled=check_cancelled)
    return store(project, fp, result), False


def public_status(session, project) -> dict:
    """{available, summary, digest, stale, job}: whether the project has what
    the measure needs, the current result, whether it is still the one
    these inputs give, and a running job."""
    out = {"available": False, "reason": None, "summary": None, "digest": None,
           "stale": False, "job": None}
    try:
        fp, _context = plan(session, project)
        out["available"] = True
    except AgentError as exc:
        fp = None
        out["reason"] = exc.message
    summary = current(project)
    if summary is not None:
        out["summary"] = summary
        out["digest"] = digest_line(summary)
        if fp is None:
            out["stale"] = True
        else:
            try:
                fp, _context = plan(session, project, reference=summary.get("reference"),
                                    params=summary.get("params"))
                out["stale"] = fp != summary.get("fingerprint")
            except AgentError:
                out["stale"] = True
    running = _read_json(_folder(project) / "running.json") or {}
    if running.get("job_id"):
        from plexora.agent import jobs

        record_job = jobs.store().get(running["job_id"])
        if record_job and record_job.get("status") in ("queued", "running"):
            out["job"] = {k: record_job.get(k) for k in ("job_id", "status", "progress")}
    return out


def note_running(project, job_id):
    path = _folder(project) / "running.json"
    if job_id:
        _atomic_write(path, json.dumps({"job_id": job_id}).encode("utf-8"))
    else:
        try:
            path.unlink()
        except OSError:
            pass
