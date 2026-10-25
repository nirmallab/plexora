"""A saved channel row carries its place in the sidebar.

The channel list is written one row per image channel, in image order, so
the order the user dragged the rows into was lost on every reload. The
sidebar now sends each active row's `position` in `list_meta`; `_row_meta`
keeps it only when it is a small non-negative integer, and the sidebar sorts
restored rows by it (viewerSidebar.applySavedChannels).
"""

import pytest

from plexora.server.models import data_model


def test_a_position_is_kept():
    assert data_model._row_meta({"position": 3}) == {"position": 3}


def test_position_rides_with_the_other_fields():
    meta = data_model._row_meta({"source": "user", "curve": "linear", "position": 0})
    assert meta == {"source": "user", "curve": "linear", "position": 0}


@pytest.mark.parametrize("bad", [-1, 1000, 2.5, "2", True, None, [1]])
def test_anything_else_is_dropped(bad):
    assert "position" not in data_model._row_meta({"position": bad})


def test_the_sidebar_sends_and_reads_it():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "plexora" / "client" / "src"
              / "js" / "views" / "viewerSidebar.js").read_text(encoding="utf-8")
    assert "position: slot.index," in source
    assert "a.row.position" in source
