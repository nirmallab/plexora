"""Every reading of a stain picture (`stain.py`), whoever asked for it: QC's
session or automatic gating. One row per reading in the QC store's
`stain_reads` table, so a marker's stain is read once per image and any
later QC session or gating run reuses it.

A table, not the QC document: writing the document changes its revision,
which keys the cell exclusions and every gating cache over them
(`exclusions._record`) -- a gating run that read a stain mid-session would
drop its own caches and call its written gates stale.

A reading belongs to an attempt (one session's readings of one channel, in
order); the channel's verdict is its latest attempt settled by
`stain.settle`. Freshness has two keys:

- `hard`: the renderer, the picture's version, the image, the channel and
  the nuclear channels -- what the picture is of. A reading under another
  hard key is never reused.
- `soft`: the partner drawn in row E, the marker graph's version and the cell
  table -- what helps read it. Under another soft key a `real` stands (a
  working stain stays working), anything else is read again.
"""

from __future__ import annotations

import hashlib
import time

import polars as pl

TABLE = "stain_reads"
SCHEMA = {"channel": pl.Utf8, "attempt": pl.Utf8, "read": pl.Int64, "verdict": pl.Utf8,
          "structure": pl.Utf8, "expectation": pl.Utf8, "observed": pl.Utf8, "why": pl.Utf8,
          "picture": pl.Utf8, "partner": pl.Utf8, "hard": pl.Utf8, "soft": pl.Utf8,
          "source": pl.Utf8, "at": pl.Float64}
SOURCES = ("qc", "gating")


def _digest(*parts) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()) \
        .hexdigest()[:16]


def keys(session, project, channel, *, partner=None, nuclear=()) -> dict:
    """{hard, soft} for a reading of `channel` on `project` now."""
    from plexora.ai import vocabulary
    from plexora.plugins.qc.server import stain
    from plexora.plugins.qc.server.cells import inputs as cell_inputs

    try:
        table = cell_inputs.table_fingerprint(session.project(project))
    except Exception:  # noqa: BLE001 -- no table: the soft key says so
        table = None
    try:
        graph = vocabulary.load()["version"]
    except Exception:  # noqa: BLE001
        graph = None
    return {"hard": _digest(stain.RENDERER, stain.VERSION, image_identity(session, project),
                            channel, ",".join(sorted(nuclear or ()))),
            "soft": _digest(partner, graph, table)}


def image_identity(session, project) -> str:
    """The image's source and its file's fingerprint (size, modification
    time): a replaced image is another picture."""
    import json

    from plexora.server.providers.base import Fingerprint

    src = getattr(session.project(project).image, "src", None)
    try:
        found = Fingerprint.of_path(src) if src else None
    except Exception:  # noqa: BLE001
        found = None
    return json.dumps([str(src), found.to_dict() if found is not None else None],
                      sort_keys=True, default=str)


def _frame(project) -> pl.DataFrame:
    from plexora.plugins.qc.server import results

    frame = results.get_table(project, TABLE, SCHEMA)
    return frame if frame is not None else pl.DataFrame(schema=SCHEMA)


def append(project, *, channel, attempt, read, answer, picture, partner, keys, source):
    """Store one reading. `answer`: {verdict, structure, expectation, observed, why}."""
    from plexora.plugins.qc.server import results

    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    row = {"channel": channel, "attempt": attempt, "read": int(read),
           "verdict": answer.get("verdict"), "structure": answer.get("structure"),
           "expectation": answer.get("expectation"), "observed": answer.get("observed"),
           "why": answer.get("why"), "picture": picture, "partner": partner,
           "hard": keys["hard"], "soft": keys["soft"], "source": source, "at": time.time()}
    with results.lock(project):
        frame = _frame(project)
        frame = pl.concat([frame, pl.DataFrame([row], schema=SCHEMA)], how="vertical")
        results.put_table(project, TABLE, frame)


def readings(project, channel, attempt) -> list:
    frame = _frame(project).filter((pl.col("channel") == channel)
                                   & (pl.col("attempt") == attempt)).sort("read")
    return frame.to_dicts()


def settled(rows, *, rereads=True) -> dict:
    """{verdict, contested, readings} of one attempt's rows (in read order).
    `verdict` is None while a reading is due. `contested`: some reading said
    `failed` and the channel did not settle `failed` -- a flag a person
    should see even when best of three called it weak."""
    from plexora.plugins.qc.server import stain

    verdicts = [r["verdict"] for r in rows]
    verdict = stain.settle(verdicts, rereads=rereads)
    return {"verdict": verdict,
            "contested": verdict is not None and verdict != "failed" and "failed" in verdicts,
            "readings": [{k: r.get(k) for k in ("read", "verdict", "structure", "why", "source")}
                         for r in rows]}


def lookup(session, project, channels, *, nuclear=(), partners=None) -> dict:
    """{channel: {verdict, contested, readings, fresh, source, picture, attempt}}
    for each of `channels` with a reusable settled reading. `fresh` is `fresh`
    (both keys match) or `soft` (only the hard key matches; kept only when the
    verdict is `real`). A channel with nothing reusable is left out."""
    frame = _frame(project)
    if frame.is_empty():
        return {}
    out = {}
    for channel in channels:
        rows = frame.filter(pl.col("channel") == channel)
        if rows.is_empty():
            continue
        now = keys(session, project, channel, partner=(partners or {}).get(channel),
                   nuclear=nuclear)
        rows = rows.filter(pl.col("hard") == now["hard"])
        if rows.is_empty():
            continue
        for attempt in (rows.group_by("attempt").agg(pl.col("at").max())
                        .sort("at", descending=True)["attempt"].to_list()):
            mine = rows.filter(pl.col("attempt") == attempt).sort("read").to_dicts()
            found = settled(mine)
            if found["verdict"] is None:
                continue          # an attempt left unfinished: an older one may stand
            fresh = "fresh" if mine[-1]["soft"] == now["soft"] else "soft"
            if fresh == "soft" and found["verdict"] != "real":
                break             # what helped read it changed: read it again
            out[channel] = {**found, "fresh": fresh, "source": mine[-1]["source"],
                            "picture": mine[-1]["picture"], "attempt": attempt,
                            "partner": mine[-1]["partner"]}
            break
    return out


def nuclear_of(session, project) -> list:
    from plexora.agent import presets

    names = [c.get("fullname") or c.get("name")
             for c in session.project(project).image.real_channels]
    return list(presets.nuclear_channels(names))


def record(session, project, channel, *, attempt, read, answer, picture, partner, source):
    """`append` with this reading's keys worked out. Never raises: a ledger
    that cannot be written loses the reuse, not the reading."""
    import logging

    try:
        append(project, channel=channel, attempt=attempt, read=read, answer=answer,
               picture=picture, partner=partner,
               keys=keys(session, project, channel, partner=partner,
                         nuclear=nuclear_of(session, project)),
               source=source)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("stain reading not stored for %s/%s", project,
                                            channel, exc_info=True)


def reusable(session, project, channel, *, partner=None) -> dict | None:
    """The settled verdict `lookup` finds for one channel, or None."""
    try:
        return lookup(session, project, [channel], nuclear=nuclear_of(session, project),
                      partners={channel: partner}).get(channel)
    except Exception:  # noqa: BLE001 -- no ledger: read it
        return None
