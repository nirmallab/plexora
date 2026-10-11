"""What QC found about one marker, in a line gating can carry.

Automatic gating used to re-judge by eye what QC had already measured -- a
channel's antibody aggregates, a broad background, a mask that holds only
nuclei -- and judged it differently each run. This module turns the stored
QC result into two things gating reads through the core seam
(`plexora/agent/cell_exclusions.py`, `ExclusionRecord.notes` / `.verdict`):

- `channel_verdict`: `usable`, `failed` or `unknown` -- core's words, mapped
  from QC's channel status (a channel `flagged` because a region reached it
  is still a working stain);
- `marker_digest`: a fixed-order `key=value; ...` line of at most
  `MAX_CHARS`, numbers to three significant figures, every field omitted when
  it has nothing to say. Templated text only, never an agent's prose: the same
  stored result always gives the same bytes, so a packet that carries it is
  replayed from memory (gating's answer memo) instead of re-asked.

Pure functions of the stored result (and the per-cell calls frame, for the
left-out counts): nothing is measured here.
"""

from __future__ import annotations

#: The longest digest, in characters; cut at a field boundary.
MAX_CHARS = 300

#: QC channel status -> core's verdict (`cell_exclusions.VERDICTS`).
VERDICT_OF_STATUS = {"clean": "usable", "flagged": "usable", "failed": "failed",
                     "manual_review": "unknown", "not_reviewed": "unknown",
                     "not_scanned": "unknown"}

#: What each status says, in the digest's `status` field.
STATUS_WORDS = {"clean": "clean", "flagged": "flagged_by_region", "failed": "failed_stain",
                "manual_review": "needs_review", "not_reviewed": "not_reviewed",
                "not_scanned": "not_scanned"}


def open_hints(entry) -> dict:
    """{class: summary} of the scan's staining hints on a channel that its
    audit left open: none when the audit called the channel clean, or
    uncertain only about outlined regions (its staining is then settled as on
    a clean row) -- the reviewer saw the tile, and a dense real population
    bears a speck hint out as well as an aggregate does; only the class it
    named when it called the channel suspicious; all of them when nobody
    audited it, or the doubt was about the staining itself (`elsewhere`)."""
    hints = (entry or {}).get("scan_hints") or {}
    audit = (entry or {}).get("audit") or {}
    verdict = audit.get("verdict")
    where = [w for w in audit.get("where") or () if w]
    if verdict == "clean" or (verdict == "uncertain" and where and "elsewhere" not in where):
        return {}
    named = audit.get("class_hint")
    if verdict == "suspicious" and named:
        return {k: v for k, v in hints.items() if k == named}
    return dict(hints)


def _num(value) -> str:
    return f"{float(value):.3g}"


def _channel(result, marker, markers=None):
    """The result's channel entry for a table marker (`vocabulary.match_name`:
    exact name, folded, else the one channel naming the same marker), or None."""
    from plexora.ai import vocabulary

    channels = (result or {}).get("channels") or []
    name = vocabulary.match_name(marker, [str(e.get("name") or "") for e in channels])
    return next((e for e in channels if str(e.get("name") or "") == name), None) \
        if name is not None else None


def channel_verdict(result, marker, markers=None) -> str:
    """`usable`, `failed` or `unknown` for one marker's channel."""
    entry = _channel(result, marker, markers)
    if entry is None:
        return "unknown"
    return VERDICT_OF_STATUS.get(entry.get("status") or "not_reviewed", "unknown")


def _fields(result, marker, *, left_out=None, findings=None):
    entry = _channel(result, marker) or {}
    cells = (result or {}).get("cells") or {}
    out = []
    status = entry.get("status")
    if status:
        out.append(("status", STATUS_WORDS.get(status, status)))
    flags = sorted(str(f) for f in entry.get("flags") or ())
    if flags:
        out.append(("scan", ",".join(flags[:3])))
    hints = open_hints(entry)
    unconfirmed = []
    for klass in sorted(hints):
        hint = hints[klass] or {}
        n = hint.get("n_grid_cells") or hint.get("n")
        unconfirmed.append(f"{klass}:{int(n)}" if n is not None else klass)
    if unconfirmed:
        out.append(("unconfirmed", ",".join(unconfirmed[:2])))
    channel_findings = ((cells.get("channel_findings") or {}).get(marker)) or findings or {}
    rim = channel_findings.get("rim") or {}
    if rim.get("borne_out") and not rim.get("dismissed"):
        out.append(("rim", f"{int(rim.get('n_flagged') or 0)}cells"))
    background = channel_findings.get("background") or {}
    if background.get("flagged"):
        out.append(("background", f"z{_num(background.get('z') or 0)}"))
    for cycle in cells.get("dna_cycles") or ():
        if marker in (cycle.get("markers") or ()):
            retained = cycle.get("cumulative_retained")
            if retained is not None and retained < 1.0:
                out.append(("cycle", f"{cycle.get('position')}:retained{_num(retained)}"))
            break
    seg = cells.get("seg_notice") or {}
    if seg.get("notice"):
        pct = seg.get("peaks_on_labels_pct")
        out.append(("mask", f"peaks_on_labels{_num(pct)}%" if pct is not None
                    else "nuclei_may_be_missing"))
    if left_out:
        reasons = ",".join(r for r, _n in left_out.get("top") or ())
        text = f"{int(left_out.get('n') or 0)}"
        out.append(("left_out", f"{text}({reasons})" if reasons else text))
    return out


def marker_digest(result, marker, *, left_out=None, findings=None) -> str:
    """The digest line for one marker ("" when QC has nothing to say).
    `left_out` = {n, top: [(reason, n), ...]} counts from the calls frame."""
    parts = [f"{key}={value}" for key, value in _fields(result, marker, left_out=left_out,
                                                        findings=findings)]
    line = ""
    for part in parts:
        candidate = part if not line else f"{line}; {part}"
        if len(candidate) > MAX_CHARS:
            break
        line = candidate
    return line
