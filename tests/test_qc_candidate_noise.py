"""Candidate noise on large multiplexed images: what a clean audit settles.

A live 40-channel CyCIF run spent most of its looks confirming candidates
that were real biology: every candidate's score saturated at 1.0, so the
audit's "strong and too small for a tile" exemption covered all of them;
autofluorescence channels raised aggregates; dense stained tissue merged
across many markers read as debris. Pinned here: the exemption uses a
non-saturating strength and a size measured on the tile, an autofluorescence
channel raises no aggregate, dense multi-marker tissue is scored down while
debris bright in the unstained channel is not, and the raw specks are capped
with the drop counted.
"""

import numpy as np

from plexora.agent import AgentSession
from plexora.plugins.qc.server import candidates as cand, scan, schemas, transitions
from plexora.plugins.qc.server.detectors.base import Candidate, DetectorContext
from plexora.plugins.qc.server.detectors.classical import AggregateDetector
from tests.qc_fixtures import SCAN_PARAMS, make_qc_project


def _raw(klass, channels, mask, *, score=1.0, strength=None, detector="aggregate"):
    return Candidate(detector=detector, detector_version="1", class_hint=klass,
                     scope_hint="channel", channels=tuple(channels), mask=mask.copy(),
                     score=score, severity=score, strength=strength)


def _unit(klass, mask, *, score=1.0, rank=None, of=None, fraction=None):
    metrics = {} if rank is None else {"strength_rank": rank, "strength_of": of}
    return {"class_hint": klass, "score": score, "metrics": metrics,
            "mask": cand.encode_mask(mask),
            "measurement": {"tissue_fraction": float(mask.mean()) if fraction is None
                            else fraction, "cells": int(mask.sum())}}


def _rename(result, old, new):
    """The scan with channel `old` called `new` (its maps and its meta)."""
    for key in [k for k in result.maps if k.startswith(f"{old}::")]:
        result.maps[f"{new}::{key.split('::', 1)[1]}"] = result.maps.pop(key)
    for channel in result.meta["channels"]:
        if channel["name"] == old:
            channel["name"] = new
    return result


def test_an_autofluorescence_channel_raises_no_aggregate(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("aggregates",))
    result, _ = scan.load_or_run(AgentSession(), info["name"], params=SCAN_PARAMS)
    found = AggregateDetector().run(DetectorContext(result, project=info["name"]))
    assert any(c.channels == ("CD8",) for c in found)  # the control: CD8 has specks
    assert all(c.strength is not None and abs(c.strength - c.metrics["z"]) <= 1e-3 * c.strength
               for c in found)
    _rename(result, "CD8", "AF2")
    found = AggregateDetector().run(DetectorContext(result, project=info["name"]))
    assert not any("AF2" in c.channels for c in found)


def test_a_clean_audit_dismisses_a_large_strong_candidate_but_keeps_a_small_one():
    """Both score 1.0 and rank first; only the one too small for the tile is
    looked at on a clean row."""
    grid = np.zeros((600, 600), dtype=bool)
    large = grid.copy()
    large[100:400, 100:400] = True          # a quarter of the image: the tile shows it
    small = grid.copy()
    small[50:52, 50:52] = True              # 4 map cells: well under one tile pixel
    for klass in ("antibody_aggregate", "debris_or_foreign_object", "saturation_or_clipping"):
        assert not transitions._forced(_unit(klass, large, rank=1, of=50)), klass
        assert transitions._forced(_unit(klass, small, rank=1, of=50)), klass
    # A misregistration is invisible on one channel's tile at any size.
    assert transitions._forced(_unit("cross_cycle_registration_error", large, rank=1, of=1))
    # Under 2% of the tissue but a visible patch on the tile: settled by it.
    medium = grid.copy()
    medium[10:40, 10:40] = True             # 900 cells = ~164 tile pixels
    assert not transitions._forced(_unit("debris_or_foreign_object", medium, rank=1, of=50,
                                         fraction=0.01))


def test_the_exemption_uses_a_measure_that_does_not_saturate():
    """Forty candidates all score 1.0; only the strongest few by their
    unbounded strength stay exempt."""
    small = np.zeros((600, 600), dtype=bool)
    small[5:7, 5:7] = True
    of = 40
    top = max(1, int(np.ceil(schemas.ENGINE["force_confirm_top_share"] * of)))
    assert transitions._forced(_unit("antibody_aggregate", small, rank=1, of=of))
    assert transitions._forced(_unit("antibody_aggregate", small, rank=top, of=of))
    assert not transitions._forced(_unit("antibody_aggregate", small, rank=top + 1, of=of))
    # Below the force-confirm score nothing is exempt, whatever its rank.
    assert not transitions._forced(_unit("antibody_aggregate", small, score=0.5, rank=1, of=of))
    # A unit recorded before the rank existed falls back to its score.
    assert transitions._forced(_unit("antibody_aggregate", small))


def test_build_ranks_by_strength_not_by_the_saturated_score(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("aggregates",))
    result, _ = scan.load_or_run(AgentSession(), info["name"], params=SCAN_PARAMS)
    shape = tuple(result.grid["shape"])
    raw = []
    for k, z in enumerate((40.0, 12.0, 25.0)):
        mask = np.zeros(shape, dtype=bool)
        mask[2 + 4 * k, 2] = True
        raw.append(_raw("antibody_aggregate", ["CD3"], mask, strength=z))
    built = cand.build(raw, result, project=info["name"])
    ranks = {c.strength: c.metrics["strength_rank"] for c in built["ranked"]}
    assert ranks == {40.0: 1, 25.0: 2, 12.0: 3}
    assert all(c.metrics["strength_of"] == 3 for c in built["ranked"])


def test_raw_specks_are_capped_per_channel_and_the_drop_counted(tmp_path):
    info = make_qc_project(tmp_path)
    result, _ = scan.load_or_run(AgentSession(), info["name"], params=SCAN_PARAMS)
    shape = tuple(result.grid["shape"])
    cap = 5
    raw = []
    for k in range(cap + 3):
        mask = np.zeros(shape, dtype=bool)
        mask[2 + (k // 6) * 3, 2 + (k % 6) * 3] = True
        raw.append(_raw("antibody_aggregate", ["CD3"], mask, strength=float(k)))
    kept, capped = cand.cap_raw(raw, per_channel=cap)
    assert sorted(c.strength for c in kept) == [3.0, 4.0, 5.0, 6.0, 7.0]  # the strongest
    assert len(capped) == 3
    rows = cand.summarise_residual([], result, capped=capped)
    assert rows[0]["n"] == 3 and rows[0]["capped"] == 3 and rows[0]["channel"] == "CD3"


class _Scan:
    """Just enough of a ScanResult for `candidates.dense_tissue`."""

    def __init__(self, shape, channels, bright):
        self.shape = shape
        self.channels = [{"name": c, "flags": []} for c in channels]
        self.maps = {}
        for name in channels:
            compact = np.zeros(shape, dtype=np.float32)
            compact[bright.get(name, np.zeros(shape, dtype=bool))] = 0.3
            self.maps[f"{name}::bright_compact"] = compact

    def map(self, channel, metric):
        return self.maps.get(f"{channel}::{metric}")

    def tissue(self, core=False):
        return np.ones(self.shape, dtype=bool)


def test_dense_multi_marker_tissue_is_scored_down_but_debris_is_not():
    shape = (40, 40)
    region = np.zeros(shape, dtype=bool)
    region[10:14, 10:14] = True
    markers = [f"M{i}" for i in range(8)]
    channels = ["DAPI", "AF1", *markers]

    def group():
        members = [_raw("antibody_aggregate", [m], region) for m in markers]
        merged = cand.merge(members)
        assert len(merged) == 1 and merged[0].class_hint == "debris_or_foreign_object"
        return merged[0]

    # Bright in eight markers, dark in the autofluorescence channel: tissue.
    tissue_scan = _Scan(shape, channels, {m: region for m in markers})
    assert cand.dense_tissue(group(), tissue_scan)
    # The same, glowing in the autofluorescence channel too: debris.
    debris_scan = _Scan(shape, channels, {**{m: region for m in markers}, "AF1": region})
    assert cand.dense_tissue(group(), debris_scan) is None
    # No autofluorescence channel: bright in every stained marker is debris,
    # in only some of them is tissue.
    no_af = ["DAPI", *markers, *[f"N{i}" for i in range(4)]]
    assert cand.dense_tissue(group(), _Scan(shape, no_af, {})) is not None
    assert cand.dense_tissue(group(), _Scan(shape, ["DAPI", *markers], {})) is None
    # A group a saturation detector also raised is never scored down.
    mixed = cand.merge([*[_raw("antibody_aggregate", [m], region) for m in markers],
                        _raw("saturation_or_clipping", ["M0"], region, detector="saturation")])
    assert cand.dense_tissue(mixed[0], tissue_scan) is None
