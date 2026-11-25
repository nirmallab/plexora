"""A whole-image check's verdict read again by readers that never saw it.

One reader's word on a whole picture decided whether a stain failed, and the
same picture got different words in different runs: lsp35766_p193_2 read
three times (2026-11-23), PD1 "technical_failure" twice and "cannot_tell"
once, KLF2 the other way round, bCAT's T1 strip "ok" twice and "cannot_tell"
once -- each flip moving its gate by 1 to 2.8. A check (`CHECK_KINDS`) is
therefore answered twice, each time by a worker of its own
(`gating.image_reader_2`; `Task.reader_of`), and a third time when the two
differ (`gating.image_reader_3`); the verdict two give stands, and with no
two alike the check's abstention (`cannot_tell`), which every check already
routes to its careful path.

A majority makes the verdict the same from run to run, not right: Rb on that
image was called failed by two readers of three, on a sheet whose gate marked
a glow and the tissue's edges while its weak nuclear stain was too dim to see.

The unit holds its readings (`verdict_reads`) until they settle; it stays in
the state that asks the check, so the engine draws the check again, tagged
with its reading (`evidence.check`, the task it routes to; the answer memo
keeps each reading apart). `SessionOptions.check_rereads` turns it off.

One reading is enough where nothing a second could say would change the
outcome: a `qc_confirm` read `real_signal` on a marker whose stain QC's stain
check read `real` (`agreed_real`).
"""

from __future__ import annotations

#: The checks read again, and the verdict each falls back to when no two
#: readers agree (the check's own "could not tell").
CHECK_KINDS = {"qc_confirm": "cannot_tell", "t1_strip": "cannot_tell"}


def read_due(unit, kind=None) -> int:
    """Which reading of its check the unit waits for: 1, or 2 and 3 while a
    check is read again (of `kind`, when given)."""
    reads = unit.get("verdict_reads")
    if not reads or (kind is not None and reads["kind"] != kind):
        return 1
    return len(reads["verdicts"]) + 1


def reading_of(packet) -> int:
    """The reading a check's packet is (its `evidence.check`), 1 untagged."""
    check = str((packet.get("evidence") or {}).get("check") or "")
    return int(check.rsplit("_", 1)[1]) if check.startswith("reader_") else 1


def tag(packet, kind, units) -> None:
    """Tag a check's packet with the reading its units wait for: the task it
    goes to (`gating.image_reader_<n>`), a worker that never saw it."""
    read = max((read_due(u, kind) for u in units), default=1)
    if read > 1:
        packet.setdefault("evidence", {})["check"] = f"reader_{read}"


def combine(verdicts, abstain):
    """The verdict of a check's readings, or None while another is due: two
    alike stand; two that differ ask for a third; of three, the one two
    gave, else `abstain`."""
    if len(verdicts) < 2:
        return None
    if len(verdicts) == 2:
        return verdicts[0] if verdicts[0] == verdicts[1] else None
    for v in verdicts:
        if verdicts.count(v) >= 2:
            return v
    return abstain


def agreed_real(unit, kind, verdicts) -> bool:
    """Whether a first `qc_confirm` reading of `real_signal` stands without a
    second: QC's stain check (two blind readers of its own picture) read the
    stain `real` too. With that verdict gating's check cannot close the
    marker failed anyway (`stain_link.blocks_failure`): more readers could
    only send it to a person. The readers are still never told the stain
    verdict; it is weighed only after they answer."""
    from plexora.plugins.gating.server.autogate import stain_link

    return kind == "qc_confirm" and verdicts == ["real_signal"] and stain_link.category(unit) == "real"


def settle(engine, unit, packet, verdict):
    """The verdict the unit acts on for this reading of its check, or None
    while it waits for another reading. Off (`check_rereads`), the reading
    stands. A reading the unit is not waiting for (a packet from before its
    check settled) is not counted, and leaves it waiting as it was."""
    kind = packet.get("kind")
    if kind not in CHECK_KINDS or engine.options["check_rereads"] == "off":
        return verdict
    read = reading_of(packet)
    name = f"{unit['project']}::{unit['marker']}"
    if read == 1:
        unit["verdict_reads"] = {"kind": kind, "verdicts": [], "packets": []}
    reads = unit.get("verdict_reads")
    if reads is None or reads["kind"] != kind or len(reads["verdicts"]) + 1 != read:
        engine.log(event="verdict_dropped", unit=name, kind=kind,
                   packet_id=packet.get("packet_id"), read=read)
        return None
    reads["verdicts"].append(verdict)
    reads["packets"].append(packet.get("packet_id"))
    settled = combine(reads["verdicts"], CHECK_KINDS[kind])
    if settled is None and agreed_real(unit, kind, reads["verdicts"]):
        settled = verdict
        engine.log(event="verdict_agreed", unit=name, kind=kind, stain="real")
    engine.log(event="verdict_read", unit=name, kind=kind, read=read, verdict=verdict,
               settled=settled)
    if settled is None:
        return None
    unit.pop("verdict_reads")
    unit.setdefault("verdict_history", []).append({**reads, "settled": settled})
    return settled
