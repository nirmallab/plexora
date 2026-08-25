"""Naming an image's channels from a file the user has.

A multiplexed image arrives with its channels called Channel_0 … Channel_n and
the panel that says what they really are lives somewhere else -- a CSV, or far
more often a spreadsheet. Until that list is in, gating matches markers to
channels by NAME and so matches nothing.

The reading is server-side for both ways in, and that is the point of
`server/utils/channel_file.py`: the other way in is a path, because on a
cluster the marker list sits beside the image on a filesystem the browser
cannot see. One reader means the upload and the path cannot disagree about
what a file says.

Three answers the route can give, and the tests below are grouped by them:

  applied       the file said which names it holds without being asked
  needs_column  it did not, so the column picker gets a description of it
  mismatch      the names were read and there is the wrong number of them,
                so NOTHING was changed

The modal that asks the questions is tests/test_channel_names_modal.py.
"""

import io
import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import tifffile
from openpyxl import Workbook

import plexora
from plexora import datasource
from plexora.server.utils import channel_file
from tests.helpers import use_data_root


def _register(tmp_path, monkeypatch, name="panel_sample"):
    """A two-channel image with a feature table, as the viewer would have it."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)

    image_path = tmp_path / "image.tif"
    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))

    h5ad_path = tmp_path / "cells.h5ad"
    obs = pd.DataFrame(index=[f"cell_{i}" for i in range(10)])
    var = pd.DataFrame(index=["MarkerA", "MarkerB"])
    adata = ad.AnnData(
        X=np.random.default_rng(0).random((10, 2)).astype(np.float32), obs=obs, var=var)
    adata.obsm["spatial"] = np.stack(
        [np.linspace(10, 50, 10), np.linspace(10, 50, 10)], axis=1)
    adata.write_h5ad(h5ad_path)

    datasource.register_anndata_datasource(
        name=name, image=image_path, features=h5ad_path,
        coordinate_source="obsm", obsm_key="spatial", data_dir=data_dir,
    )
    return data_dir


def _names_in(data_dir, name="panel_sample"):
    config = json.loads((data_dir / "config.json").read_text())
    return [c["fullname"] for c in config[name]["imageData"]]


def _grid(payload, filename="markers.csv"):
    return channel_file.read_grid(data=payload, filename=filename)


def _xlsx(rows):
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# -- reading the file --------------------------------------------------------


def test_a_one_column_list_is_read_as_it_stands():
    grid = _grid(b"DAPI\nCD3\n")
    assert channel_file.autodetect(grid, 2) is False, "no header to drop"
    assert channel_file.names(grid, 0, False) == ["DAPI", "CD3"]


def test_a_header_row_comes_off_only_when_dropping_it_makes_the_count_right():
    """`marker` is not a channel name and `CD3` is, and nothing about either
    string says so. The row count against the image's is the only evidence
    there is, which is why one more row than channels means a header and the
    same number of rows means there is not one."""
    grid = _grid(b"marker\nDAPI\nCD3\n")
    assert channel_file.autodetect(grid, 2) is True
    assert channel_file.names(grid, 0, True) == ["DAPI", "CD3"]
    # The same file against a three-channel image: three rows, three channels,
    # so "marker" is a channel called marker. An unlikely file, and still the
    # only reading the evidence supports.
    assert channel_file.autodetect(grid, 3) is False


def test_a_count_that_matches_neither_reading_is_not_guessed_at():
    grid = _grid(b"DAPI\nCD3\nCD8\nCD20\n")
    assert channel_file.autodetect(grid, 2) is None


def test_blank_rows_and_trailing_blank_columns_are_not_channels():
    """A trailing newline, a spacer line, and the empty columns a spreadsheet
    reports forever after somebody once typed in column D. Each would
    otherwise count as a channel the image has not got."""
    grid = _grid(b"DAPI,,\n\nCD3,,\n\n")
    assert grid == [["DAPI"], ["CD3"]]
    assert channel_file.width(grid) == 1


def test_an_empty_column_cell_is_not_a_channel_called_nothing():
    grid = _grid(b"id,marker\n1,DAPI\n2,\n3,CD3\n")
    assert channel_file.names(grid, 1, True) == ["DAPI", "CD3"]


def test_a_tab_separated_file_is_read_without_being_told():
    grid = _grid(b"cycle\tmarker\n1\tDAPI\n", "panel.tsv")
    assert grid == [["cycle", "marker"], ["1", "DAPI"]]


def test_a_spreadsheet_is_read_like_a_csv():
    """The format a panel design is actually written in. Everything downstream
    of read_grid is format-blind, which is the whole reason it is one call."""
    grid = _grid(_xlsx([["marker"], ["DAPI"], ["CD3"]]), "panel.xlsx")
    assert grid == [["marker"], ["DAPI"], ["CD3"]]
    assert channel_file.autodetect(grid, 2) is True


def test_a_spreadsheet_number_is_read_as_the_text_of_the_cell():
    """Cycle numbers land as ints and channel names sometimes are digits. The
    grid is strings throughout so a column of them can be picked like any
    other, rather than arriving as `1.0`."""
    grid = _grid(_xlsx([["cycle", "marker"], [1, "DAPI"], [2, "CD3"]]), "panel.xlsx")
    assert grid[1] == ["1", "DAPI"]


def test_formats_nothing_here_can_read_are_refused_by_name():
    with pytest.raises(channel_file.ChannelFileError, match="not a .csv"):
        _grid(b"%PDF-1.4", "notes.pdf")
    # .xls is refused with the fix rather than with the list of what is taken:
    # the file IS a spreadsheet, it is just the format neither reader opens.
    with pytest.raises(channel_file.ChannelFileError, match="save it as .xlsx"):
        _grid(b"\xd0\xcf\x11\xe0", "panel.xls")


def test_a_file_far_too_long_to_be_a_channel_list_is_refused_early():
    """A feature table dropped in by mistake. The refusal is the point; not
    reading a million rows to reach it is why the cap is in the reader."""
    payload = b"\n".join(b"row%d" % i for i in range(channel_file.MAX_ROWS + 5))
    with pytest.raises(channel_file.ChannelFileError, match="not a list of channel names"):
        _grid(payload)


def test_an_empty_file_says_so():
    with pytest.raises(channel_file.ChannelFileError, match="empty"):
        _grid(b"\n\n\n")


# -- describing it for the picker --------------------------------------------


def test_a_table_of_several_columns_is_never_guessed_at():
    grid = _grid(b"cycle,marker\n1,DAPI\n2,CD3\n")
    assert channel_file.autodetect(grid, 2) is None, (
        "`cycle` and `marker` both have two names under a header -- picking "
        "between them is the user's, and getting it wrong renames every channel")


def test_the_description_carries_the_count_the_user_is_comparing():
    grid = _grid(b"cycle,marker,note\n1,DAPI,nuclear\n2,CD3,\n")
    described = channel_file.describe(grid, 2, "panel.csv")
    assert [c["header"] for c in described["columns"]] == ["cycle", "marker", "note"]
    # Every non-empty cell, header included -- the modal takes the header off
    # itself when the checkbox is ticked, which is what makes ticking it
    # instant rather than another request.
    assert [c["nonempty"] for c in described["columns"]] == [3, 3, 2]
    assert described["channel_count"] == 2
    assert described["column_count"] == 3


def test_the_header_guess_is_the_reading_that_would_fit():
    """Not a claim about the file -- the checkbox is the user's to change. It
    is the same reasoning autodetect uses on one column, applied to a table."""
    with_header = channel_file.describe(_grid(b"cycle,marker\n1,DAPI\n2,CD3\n"), 2)
    assert with_header["header_guess"] is True
    without = channel_file.describe(_grid(b"1,DAPI\n2,CD3\n"), 2)
    assert without["header_guess"] is False


def test_the_preview_is_small_enough_to_read():
    rows = b"\n".join(b"c%d,DAPI,x,y,z,w,v" % i for i in range(40))
    described = channel_file.describe(_grid(rows), 40)
    assert len(described["preview"]) == channel_file.PREVIEW_ROWS
    assert all(len(row) <= channel_file.PREVIEW_COLUMNS for row in described["preview"])
    # The full width is still reported, because the modal has to say the
    # preview is only part of the file.
    assert described["column_count"] == 7


def test_a_table_far_too_wide_to_choose_from_is_capped():
    header = b",".join(b"col%d" % i for i in range(channel_file.MAX_COLUMNS + 10))
    described = channel_file.describe(_grid(header + b"\n" + header), 2)
    assert len(described["columns"]) == channel_file.MAX_COLUMNS
    assert described["columns_truncated"] is True


# -- the route ---------------------------------------------------------------


def _post(client, name="panel_sample", **fields):
    return client.post(
        "/upload_channels",
        data={"datasource": name, **fields},
        content_type="multipart/form-data",
    )


def test_a_file_that_answers_for_itself_is_applied_in_one_request(tmp_path, monkeypatch):
    data_dir = _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client(),
                     file=(io.BytesIO(b"marker\nDAPI\nCD3\n"), "panel.csv"))

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert _names_in(data_dir) == ["DAPI", "CD3"]


def test_a_multi_column_file_comes_back_asking_which_column(tmp_path, monkeypatch):
    data_dir = _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client(),
                     file=(io.BytesIO(b"cycle,marker\n1,DAPI\n2,CD3\n"), "panel.csv"))

    body = response.get_json()
    # 200, not an error: the server is asking for one more thing, and a red
    # line in the modal is the wrong shape for a question.
    assert response.status_code == 200
    assert body["success"] is False and body["needs_column"] is True
    assert body["column_count"] == 2 and body["channel_count"] == 2
    assert body["preview"][0] == ["cycle", "marker"]
    assert body["filename"] == "panel.csv"
    assert _names_in(data_dir) == ["MarkerA", "MarkerB"], "asking is not applying"


def test_the_chosen_column_is_what_gets_applied(tmp_path, monkeypatch):
    data_dir = _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client(),
                     file=(io.BytesIO(b"cycle,marker\n1,DAPI\n2,CD3\n"), "panel.csv"),
                     column="1", has_header="true")

    assert response.status_code == 200
    assert response.get_json()["names"] == ["DAPI", "CD3"]
    assert _names_in(data_dir) == ["DAPI", "CD3"]


def test_the_header_answer_is_the_users_not_a_guess(tmp_path, monkeypatch):
    """Same file, same column, `has_header` false: the first row is a name.
    The checkbox has to be able to overrule what the server would have read,
    or it is decoration."""
    data_dir = _register(tmp_path, monkeypatch, name="two")
    response = _post(plexora.app.test_client(), name="two",
                     file=(io.BytesIO(b"marker\nDAPI\n"), "panel.csv"),
                     column="0", has_header="false")

    assert response.status_code == 200
    assert _names_in(data_dir, "two") == ["marker", "DAPI"]


def test_a_count_that_does_not_match_changes_nothing_and_reports_both_numbers(
        tmp_path, monkeypatch):
    """The common failure is the right kind of file for the wrong image. Half
    a panel applied would look named, and every wrong name in it would be
    believed by gating -- so it is all or nothing."""
    data_dir = _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client(),
                     file=(io.BytesIO(b"DAPI\nCD3\nCD8\nCD20\n"), "panel.csv"))

    body = response.get_json()
    assert response.status_code == 400
    assert body["mismatch"] is True
    assert body["marker_count"] == 4 and body["channel_count"] == 2
    assert _names_in(data_dir) == ["MarkerA", "MarkerB"]


def test_a_single_column_file_never_stops_to_ask_which_column(tmp_path, monkeypatch):
    """There is only one, so the picker would be a select with one option in
    front of a user whose real problem is the count. It goes straight to the
    mismatch above -- this pins that it does not detour."""
    _register(tmp_path, monkeypatch)
    # Five names against two channels: too far out for the header reading to
    # rescue, which three rows would have been (drop one, and it fits).
    body = _post(plexora.app.test_client(),
                 file=(io.BytesIO(b"DAPI\nCD3\nCD8\nCD20\nCD8a\n"), "panel.csv")).get_json()
    assert body.get("needs_column") is not True
    assert body["mismatch"] is True and body["marker_count"] == 5


def test_a_path_is_read_from_the_servers_own_disk(tmp_path, monkeypatch):
    """The HPC way in. The browser is on a laptop that cannot see this file;
    the server is on the machine that can."""
    data_dir = _register(tmp_path, monkeypatch)
    panel = tmp_path / "panel.csv"
    panel.write_text("marker\nDAPI\nCD3\n", encoding="utf-8")

    response = _post(plexora.app.test_client(), path=str(panel))

    assert response.status_code == 200
    assert _names_in(data_dir) == ["DAPI", "CD3"]


def test_a_path_that_is_not_there_says_which_one(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client(), path=str(tmp_path / "nope.csv"))
    assert response.status_code == 400
    assert "nope.csv" in response.get_json()["error"]


def test_a_quoted_path_is_still_a_path(tmp_path, monkeypatch):
    """Windows copies a path with the quotes on, and a file manager drags one
    in the same way. Same trimming as every other path input in the app."""
    data_dir = _register(tmp_path, monkeypatch)
    panel = tmp_path / "panel.csv"
    panel.write_text("DAPI\nCD3\n", encoding="utf-8")

    response = _post(plexora.app.test_client(), path=f'"{panel}"')

    assert response.status_code == 200
    assert _names_in(data_dir) == ["DAPI", "CD3"]


def test_a_spreadsheet_goes_through_the_route_like_anything_else(tmp_path, monkeypatch):
    data_dir = _register(tmp_path, monkeypatch)
    payload = _xlsx([["cycle", "marker"], [1, "DAPI"], [2, "CD3"]])
    response = _post(plexora.app.test_client(),
                     file=(io.BytesIO(payload), "panel.xlsx"),
                     column="1", has_header="true")

    assert response.status_code == 200
    assert _names_in(data_dir) == ["DAPI", "CD3"]


def test_nothing_at_all_is_asked_for_rather_than_crashed_on(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client())
    assert response.status_code == 400
    assert "paste the path" in response.get_json()["error"]


def test_an_unknown_project_is_refused_before_the_file_is_read(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    response = _post(plexora.app.test_client(), name="not_a_project",
                     file=(io.BytesIO(b"DAPI\n"), "panel.csv"))
    assert response.status_code == 422
