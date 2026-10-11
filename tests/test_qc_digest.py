"""QC's line about a marker (`plexora/plugins/qc/server/digest.py`): what gating
is told instead of re-judging by eye what QC already measured."""

from plexora.plugins.qc.server import digest


def _result():
    return {"channels": [
        {"name": "CTLA4", "status": "flagged", "flags": ["dim", "high_background"],
         "scan_hints": {"antibody_aggregate": {"n": 408, "n_grid_cells": 37}}},
        {"name": "CD3", "status": "clean"},
        {"name": "Ki67", "status": "failed"},
        {"name": "PD1", "status": "not_reviewed"}],
        "cells": {"seg_notice": {"notice": "Only 42% ...", "peaks_on_labels_pct": 42.0},
                  "dna_cycles": [{"position": 3, "markers": ["CTLA4"],
                                  "cumulative_retained": 0.91}],
                  "channel_findings": {"CTLA4": {"background": {"flagged": True,
                                                                "z": 2.71}}}}}


def test_the_digest_is_the_same_bytes_every_time_and_short():
    result = _result()
    left_out = {"n": 10768, "top": [("region:tissue_fold", 9000), ("seg_under", 900)]}
    first = digest.marker_digest(result, "CTLA4", left_out=left_out)
    assert first == digest.marker_digest(result, "CTLA4", left_out=left_out)
    assert len(first) <= digest.MAX_CHARS
    keys = [part.split("=", 1)[0] for part in first.split("; ")]
    assert keys == ["status", "scan", "unconfirmed", "background", "cycle", "mask",
                    "left_out"]
    assert "antibody_aggregate:37" in first and "z2.71" in first
    # A field with nothing to say is left out, never written empty.
    assert digest.marker_digest({"channels": [{"name": "CD3"}]}, "CD3") == ""


def test_the_digest_is_cut_at_a_field_boundary():
    result = _result()
    result["channels"][0]["flags"] = [f"flag_{i}_{'x' * 80}" for i in range(3)]
    left_out = {"n": 5, "top": [("r" * 200, 5)]}
    line = digest.marker_digest(result, "CTLA4", left_out=left_out)
    assert len(line) <= digest.MAX_CHARS and not line.endswith(";")
    assert all("=" in part for part in line.split("; "))


def test_a_channel_flagged_by_a_region_is_still_a_working_stain():
    result = _result()
    assert digest.channel_verdict(result, "CTLA4") == "usable"
    assert digest.channel_verdict(result, "CD3") == "usable"
    assert digest.channel_verdict(result, "Ki67") == "failed"
    assert digest.channel_verdict(result, "PD1") == "unknown"
    assert digest.channel_verdict(result, "FOXP3") == "unknown"


def test_staining_hints_reach_cells_only_where_the_audit_left_them_open():
    """lsp11385_2: the scan hinted specks on all 40 channels; 38 audited clean.
    A clean row (or one uncertain only about outlined regions) settles its
    staining, so a dense real population under a hint is never warned."""
    from plexora.plugins.qc.server.cells.calls import reviewed_hints

    both = {"antibody_aggregate": {"n_grid_cells": 9}, "excessive_background": {"n_grid_cells": 4}}
    result = {"channels": [
        {"name": "CD45", "status": "clean", "audit": {"verdict": "clean", "where": []},
         "scan_hints": both},
        {"name": "CD4", "status": "clean", "audit": {"verdict": "uncertain", "where": ["c1"]},
         "scan_hints": both},
        {"name": "CTLA4", "status": "flagged",
         "audit": {"verdict": "suspicious", "class_hint": "antibody_aggregate",
                   "where": ["elsewhere"]}, "scan_hints": both},
        {"name": "TIGIT", "status": "manual_review",
         "audit": {"verdict": "uncertain", "where": []}, "scan_hints": both},
        {"name": "PD1", "status": "not_reviewed", "scan_hints": both}]}
    hints = [{"channel": c, "class": k} for c in ("CD45", "CD4", "CTLA4", "TIGIT", "PD1", "LAG3")
             for k in both]
    kept = {(h["channel"], h["class"]) for h in reviewed_hints(result, hints)}
    assert kept == {("CTLA4", "antibody_aggregate"),
                    *{(c, k) for c in ("TIGIT", "PD1", "LAG3") for k in both}}
    assert "unconfirmed" not in digest.marker_digest(result, "CD45")
    line = digest.marker_digest(result, "CTLA4")
    assert "unconfirmed=antibody_aggregate:9" in line and "excessive_background" not in line
