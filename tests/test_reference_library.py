"""The reference library: cells sorted by eye, sheets built from them, shown once per reader.

Tiles are drawn exactly as a look draws its cells; a person sorts them into
class folders in the Finder; sheets are built from the labelled tiles, one
per layout; a session asked for one shows it beside the first reference
packet each reader (one conversation, one epoch) gets, and points at that
packet afterwards. A sheet rebuilt goes again to everyone; one deleted is
`missing` and the session goes on without it.
"""

import io

import numpy as np
import pytest
from PIL import Image

from plexora.agent import registry
from tests.autogate_fixtures import FakeData, make_gating_project
from tests.test_gating_session import Oracle, invoke, ok, start

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _gating(tmp_path, monkeypatch):
    registry.discover(["gating"])
    monkeypatch.setenv("PLEXORA_GATING_LIBRARY_PATH", str(tmp_path / "library"))


def _png(colour, size=(160, 80)):
    from plexora.server.utils import fast_png

    return fast_png.encode_rgb8_png(np.full((size[1], size[0], 3), colour, dtype=np.uint8))


def _tile(project, cell, marker, *, klass=None, shape="membrane", band="dim",
          density="dense", pct=70.0):
    from plexora.plugins.gating.server.autogate import library

    tid = library.tile_id(project, cell, marker)
    meta = {"tile_id": tid, "project": project, "cell_id": cell, "marker": marker,
            "band": band, "density": density, "value": float(cell), "pct": pct,
            "class": None, "shape": shape, "shape_source": "panel", "created": "now"}
    library.write_tile(meta, _png((cell % 200, 40, 90)))
    if klass:
        library.label({tid: {"class": klass}})
    return tid


def test_cells_come_from_every_band_and_both_densities_the_same_way_each_time():
    from plexora.plugins.gating.server.autogate import library

    rng = np.random.default_rng(0)
    xs = np.concatenate([rng.normal(500, 40, 6000), rng.uniform(0, 10_000, 6000)])
    ys = np.concatenate([rng.normal(500, 40, 6000), rng.uniform(0, 10_000, 6000)])
    values = np.expm1(rng.normal(5, 1, 12_000)).astype(np.float32)
    ds = FakeData({"M": values}, xs=xs, ys=ys)
    out = library.library_cells(ds, "M", n=30, seed=3)
    cells = out["cells"]
    assert len(cells) == 30 and out["empty_bands"] == []
    assert {c["band"] for c in cells} == set(library.BAND_ORDER)
    assert {c["density"] for c in cells} >= {"dense", "sparse"}
    assert len({c["cell_id"] for c in cells}) == 30
    assert library.library_cells(ds, "M", n=30, seed=3) == out
    assert library.library_cells(ds, "M", n=30, seed=4) != out
    by_band = {b: [c["pct"] for c in cells if c["band"] == b] for b in library.BAND_ORDER}
    assert min(by_band["bright"]) > max(by_band["background"])


def test_labels_ingest_and_forget(tmp_path):
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import library

    library.ensure()
    a, b, c = (_tile("p", cell, "CD3") for cell in (1, 2, 3))
    inbox = library.root() / "inbox" / "p" / "CD3"
    for klass in library.CLASSES + (library.DISCARD,):
        (inbox / klass).mkdir(parents=True, exist_ok=True)
    (inbox / "faint" / "fibrous").mkdir()
    (inbox / "faint" / "fibrous" / f"t01-{a}.png").write_bytes(_png((1, 1, 1)))
    (inbox / "negative" / f"t02-{b}.png").write_bytes(_png((1, 1, 1)))
    (inbox / f"t03-{c}.png").write_bytes(_png((1, 1, 1)))           # left loose
    dry = library.ingest_inbox(dry_run=True)
    assert len(dry["labelled"]) == 2 and dry["left_unsorted"] == 1
    assert library.read_tile(a)["class"] is None
    (inbox / "maybe").mkdir()
    with pytest.raises(AgentError) as refused:
        library.ingest_inbox()
    assert "maybe" in str(refused.value.detail)
    assert library.read_tile(a)["class"] is None                     # nothing changed
    (inbox / "maybe").rmdir()
    done = library.ingest_inbox()
    assert {t["tile_id"] for t in done["labelled"]} == {a, b}
    assert library.read_tile(a)["class"] == "faint"
    assert library.read_tile(a)["shape"] == "fibrous"
    assert library.read_tile(b)["class"] == "negative"
    assert inbox.is_dir()                                            # one tile left to sort
    assert library.tiles(labelled=True)[0] and len(library.tiles(labelled=False)[0]) == 1
    # A labelled tile is never overwritten by a new sample of the same cell.
    library.write_tile({**library.read_tile(a), "class": None}, _png((9, 9, 9)))
    assert library.read_tile(a)["class"] == "faint"
    # The index is a cache: gone or corrupt, it is rebuilt from the tiles.
    (library.root() / "index.json").write_text("{broken")
    assert len(library.tiles()[0]) == 3
    (library.root() / "tiles" / f"{b}.png").unlink()
    found, warnings = library.tiles()
    assert b not in {t["tile_id"] for t in found} and warnings
    assert library.forget([a, "nope000000"]) == {"removed": [a], "missing": ["nope000000"]}


def _sorted_library():
    tiles = []
    for i, (shape, klass, density, pct) in enumerate([
            ("membrane", "strong", "dense", 98), ("membrane", "faint", "sparse", 70),
            ("membrane", "limit", "dense", 50), ("membrane", "negative", "sparse", 20),
            ("membrane", "negative", "dense", 48), ("membrane", "spill", "dense", 40),
            ("fibrous", "faint", "dense", 75), ("fibrous", "negative", "sparse", 30),
            ("nuclear", "strong", "dense", 99)]):
        tiles.append(_tile("p", 100 + i, "CD3" if shape == "membrane" else "aSMA",
                           klass=klass, shape=shape, density=density, pct=pct))
    return tiles


def test_sheets_by_shape_and_pairs_are_deterministic_and_capped():
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import library, library_sheet

    with pytest.raises(AgentError):
        library_sheet.build("default")                               # nothing labelled yet
    _sorted_library()
    built = library_sheet.build("default")
    assert built["size"][0] <= library_sheet.MAX_WIDTH
    assert built["size"][1] <= library_sheet.MAX_HEIGHT
    labels = [r["label"] for r in built["rows"]]
    assert labels == ["membrane", "nuclear", "fibrous"]          # STAIN_SHAPES order
    assert "empty:artifact" in built["rows"][0]["tiles"]
    again = library_sheet.build("default")
    assert again["digest"] == built["digest"] and again["image"] == built["image"]
    pairs = library_sheet.build("pairs", layout="pairs")
    membrane = pairs["rows"][0]["tiles"]
    # The limit tile (pct 50) is paired with the closest negative of its
    # marker and density (pct 48), not the far one.
    limit = library.tile_id("p", 102, "CD3")
    close = library.tile_id("p", 104, "CD3")
    assert membrane[membrane.index(limit) + 1] == close
    assert library.resolve_sheet(None)["status"] == "off"
    assert library.resolve_sheet("auto")["name"] == "default"
    assert library.resolve_sheet("pairs")["digest"] == pairs["digest"]
    with pytest.raises(AgentError):
        library.resolve_sheet("nonexistent")


def test_no_sheet_is_no_error_with_auto():
    from plexora.plugins.gating.server.autogate import library

    assert library.resolve_sheet("auto")["status"] == "none"


def _reference_images(packet):
    return [i for i in packet.get("images") or () if i.get("role") == "reference_sheet"]


def test_a_session_shows_the_sheet_once_per_reader(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server.autogate import engine, library_sheet
    from tests.test_gating_session import HARD

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    _sorted_library()
    library_sheet.build("default")
    session = AgentSession()
    try:
        started = start(session, reference_sheet="default", display_check="off")
        assert started["reference"]["status"] == "on"
        sid = started["session_id"]
        agent = Oracle(info)
        result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))
        shown = []          # (packet_id, sheet attached, as_in, after a new conversation)
        fresh = False
        for _ in range(60):
            if result["state"] != "decision":
                break
            packet = result["packet"]
            if packet.get("reference_sheet"):
                attached = bool(_reference_images(packet))
                shown.append((packet["packet_id"], attached,
                              packet["reference_sheet"].get("as_in"), fresh))
                assert len(result.get("_images") or ()) == len(packet["images"]) <= 3
                if attached:
                    assert "reference_sheet" in packet["evidence"].get("guide", [])
                fresh = False
                if len(shown) == 2:
                    # A new conversation (no guide held): the sheet goes again.
                    ok(invoke(session, "gating_session_status", {"session_id": sid}))
                    fresh = True
            answered = ok(invoke(session, "gating_answer", {
                "session_id": sid, "packet_id": packet["packet_id"],
                "answer": agent.answer(packet)}))
            # The next packet's pictures ride beside it, at the top.
            result = {**answered["next"], "_images": answered.get("_images")}
        assert len(shown) >= 3, shown
        first, second, third = shown[:3]
        assert first[1] is True and first[2] is None
        assert second[1] is False and second[2] == first[0]
        assert third[1] is True and third[3] is True
        for later in shown[3:]:
            assert later[1] is False and later[2] == third[0]
        record = engine.store().load(sid)
        assert record["reference"]["sent_to"] == ["main"]
        assert record["used"].get("extra_images", 0) == sum(1 for s in shown if s[1])
        for unit in record["units"].values():
            # The sheet is the session's cost, never a marker's allowance.
            used = unit.get("used") or {}
            assert int(used.get("images", 0)) <= 2 * int(used.get("packets", 0))
    finally:
        session.close()


def test_a_missing_sheet_never_stops_a_session(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server.autogate import engine, library, library_sheet
    from tests.test_gating_session import drive

    info = make_gating_project(tmp_path, grid=24, size=1024)
    _sorted_library()
    library_sheet.build("default")
    session = AgentSession()
    try:
        started = start(session, markers=["CD3", "CD8", "CD20"], reference_sheet="auto",
                        display_check="off")
        library.forget(sheet="default")
        drive(session, started["session_id"], Oracle(info))
        record = engine.store().load(started["session_id"])
        assert all(u["state"] in engine.TERMINAL for u in record["units"].values())
        assert record["reference"]["status"] == "missing"
    finally:
        session.close()


def test_the_reference_is_in_the_memo_key_only_when_on():
    from types import SimpleNamespace

    from plexora.plugins.gating.server.autogate import memo
    from plexora.plugins.gating.server.autogate.engine import Engine

    packet = {"kind": "t2_confirm", "evidence": {"marker": "M"}, "reference_sheet": {"a": 1}}

    def key(record):
        fake = SimpleNamespace(options={"tiles": False, "observations": "off"}, record=record)
        for name in ("tiles_on", "observations_on", "vision_first_on", "partners_bands_on",
                     "shapes_on"):
            setattr(fake, name, lambda: False)
        fake.reference_on = lambda: Engine.reference_on(fake)
        return Engine.memo_key(fake, packet, [])

    off = key({})
    assert off == memo.key({k: v for k, v in packet.items() if k != "reference_sheet"}, [])
    on = key({"reference": {"status": "on", "digest": "abc"}})
    assert on != off
    assert key({"reference": {"status": "on", "digest": "abd"}}) != on
    assert key({"reference": {"status": "missing", "digest": "abc"}}) == off


def test_a_tile_is_the_looks_own_cell(tmp_path):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import library

    make_gating_project(tmp_path, grid=16, size=768)
    session = AgentSession()
    try:
        ds = session.data("gsynth")
        out = library.sample(session, "gsynth", "CD3", n=6, seed=1)
        assert len(out["tiles"]) == 6 and (library.root() / "inbox" / "gsynth" / "CD3").is_dir()
        assert (library.root() / "inbox" / "gsynth" / "CD3" / "faint").is_dir()
        first = library.read_tile(out["tiles"][0]["tile_id"])
        cell = {k: first[k] for k in ("cell_id", "x", "y", "value")}
        rendered = collage.render_collage(session, ds, layout="t2",
                                          rows=[{"label": "", "cells": [cell]}],
                                          marker=first["channel"], fmt="png", store=False,
                                          title="reference library")
        canvas = np.asarray(Image.open(io.BytesIO(rendered["png"])).convert("RGB"))
        top = collage.HEADER_H + collage.ROW_HEADER_H
        side = collage.tile_side("t2")
        block = canvas[top:top + side, :2 * side]
        tile = np.asarray(Image.open(io.BytesIO(
            library.tile_png(first["tile_id"]))).convert("RGB"))
        assert tile.shape == block.shape and np.array_equal(tile, block)
    finally:
        session.close()
