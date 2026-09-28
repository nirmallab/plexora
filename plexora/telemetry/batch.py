"""Turn queued rows into the body the Worker accepts.

The same builder serves the uploader and `plexora telemetry preview`, so what
the preview shows is exactly what would be sent. Every row is re-validated
against the schema in the mode in force *now* -- a queue filled under
diagnostics and sent after a server ceiling of anonymous loses its
diagnostics-only fields here -- and every event passes `redact` before it is
serialized.
"""

from __future__ import annotations

import json

from plexora.telemetry import redact, schema
from plexora.telemetry.queue import gzip_json

#: Rough upper bound on the raw JSON one batch may reach before gzip; the
#: real limit is the gzipped size, checked after.
RAW_SOFT_LIMIT = 480 * 1024


def _counter_row(row):
    """A queue row as `(window, event, key, dims_dict, agg, n, bins, s, mx)`."""
    window, event, key, dims, agg, n = row[:6]
    bins = list(row[6:6 + schema.HIST_BINS])
    s, mx = row[6 + schema.HIST_BINS], row[7 + schema.HIST_BINS]
    try:
        parsed = json.loads(dims)
    except ValueError:
        parsed = None
    return window, event, key, parsed, agg, int(n), bins, float(s), float(mx)


def build_events(counter_rows, records, mode, names=frozenset(), *, limit_rows=None,
                 max_events=schema.MAX_EVENTS_PER_BATCH):
    """`(events, used_counter_keys, used_record_ids, dropped)`.

    `used_*` name the source rows consumed -- including the rows that were
    rejected, which are dropped rather than retried forever.
    """
    dropped = 0
    used_counters, used_records = [], []
    events = []
    raw = 0

    # Records first: they are the rarer and the more informative.
    for rid, window, event, props, _priority in records:
        if len(events) >= max_events or raw > RAW_SOFT_LIMIT:
            break
        used_records.append(rid)
        try:
            parsed = json.loads(props)
        except ValueError:
            dropped += 1
            continue
        if event not in schema.EVENTS or not isinstance(parsed, dict):
            dropped += 1
            continue
        stripped = schema.strip_record(event, parsed, mode)
        if not schema.validate_record(event, stripped, mode):
            dropped += 1
            continue
        item = {"type": event, "window": window, "props": stripped}
        events.append(item)
        raw += len(json.dumps(item))

    # Then counters, grouped per (window, event) in queue order.
    groups: dict = {}
    order = []
    taken = 0
    for row in counter_rows:
        if limit_rows is not None and taken >= limit_rows:
            break
        window, event, key, dims, agg, n, bins, s, mx = _counter_row(row)
        group_key = (window, event)
        if group_key not in groups:
            if len(events) + len(order) >= max_events or raw > RAW_SOFT_LIMIT:
                break
            groups[group_key] = {}
            order.append(group_key)
        taken += 1
        used_counters.append(tuple(row[:4]))
        if (event not in schema.EVENTS or schema.key_agg(event, key) != agg
                or not isinstance(dims, dict)):
            dropped += 1
            continue
        stripped = schema.strip_dims(event, key, dims, mode)
        if stripped is None or not schema.validate_row(event, key, stripped, mode):
            dropped += 1
            continue
        merge_key = (key, json.dumps(stripped, sort_keys=True))
        existing = groups[group_key].get(merge_key)
        if existing is None:
            entry = {"k": key, "d": stripped, "n": n}
            if agg == "hist":
                entry["h"] = bins
                entry["s"] = s
                entry["mx"] = mx
            groups[group_key][merge_key] = entry
            raw += 40 + len(merge_key[1]) + (60 if agg == "hist" else 0)
        else:
            existing["n"] += n
            if agg == "hist":
                existing["h"] = [a + b for a, b in zip(existing["h"], bins)]
                existing["s"] += s
                existing["mx"] = max(existing["mx"], mx)

    for window, event in order:
        rows = list(groups[(window, event)].values())
        for entry in rows:
            if "s" in entry:
                entry["s"] = round(entry["s"], 1)
                entry["mx"] = round(entry["mx"], 1)
        for start in range(0, len(rows), schema.MAX_ROWS_PER_EVENT):
            chunk = rows[start:start + schema.MAX_ROWS_PER_EVENT]
            if chunk:
                events.append({"type": event, "window": window, "rows": chunk})

    kept, dirty = redact.clean_events(events, names)
    return kept, used_counters, used_records, dropped + dirty


def body(batch_id, client, events) -> dict:
    return {"schema": schema.SCHEMA_VERSION, "batch_id": batch_id, "client": client,
            "events": events}


def builder(client, mode, names, *, max_gzip=schema.MAX_BATCH_GZIP_BYTES, on_dropped=None):
    """A `build` callable for `Queue.take_batch`."""

    def build(batch_id, counter_rows, records):
        limit = len(counter_rows)
        max_events = schema.MAX_EVENTS_PER_BATCH
        while True:
            events, used_c, used_r, dropped = build_events(
                counter_rows, records, mode, names, limit_rows=limit, max_events=max_events)
            if not events:
                if on_dropped and dropped:
                    on_dropped(dropped)
                return None, used_c, used_r, 0, 0
            payload, raw = gzip_json(body(batch_id, client, events))
            # Both caps: the Worker refuses more than 200 events outright, and
            # one group of more than 500 rows is chunked into several.
            fits = len(payload) <= max_gzip and len(events) <= schema.MAX_EVENTS_PER_BATCH
            if fits or (limit <= 8 and max_events <= 8):
                if not fits:
                    # Cannot be made small enough: unsendable, so dropped
                    # rather than retried every tick.
                    if on_dropped:
                        on_dropped(len(used_c) + len(used_r))
                    return None, used_c, used_r, 0, 0
                if on_dropped and dropped:
                    on_dropped(dropped)
                return payload, used_c, used_r, len(events), raw
            limit = max(8, limit // 2)
            max_events = max(8, max_events // 2)

    return build
