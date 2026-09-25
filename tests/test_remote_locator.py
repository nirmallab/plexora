"""The web-address vocabulary: what counts as one, and its one spelling.

Every stage from the paste box to the tile route used to call `Path()` on the
image path first. `Path("https://host/x.zarr")` folds the double slash, so each
of those stages has to recognise a web address before it builds a path -- and
all of them have to agree on how the address is spelled, or one image becomes
two projects and two cache trees.
"""

import pytest

from plexora.server.providers.base import (is_addressed, is_node_locator,
                                           is_remote_locator)
from plexora.server.utils import remote_store


@pytest.mark.parametrize("value", [
    "https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.5/x.zarr",
    "HTTPS://host/x.zarr",
    "http://127.0.0.1:8000/x.zarr",
    "s3://idr/zarr/x.zarr",
    "gs://bucket/x.zarr",
    "gcs://bucket/x.zarr",
    "az://container/x.zarr",
    "abfs://container/x.zarr",
    "abfss://container/x.zarr",
    "  'https://host/x.zarr'  ".strip().strip("'"),
])
def test_web_addresses_are_recognised(value):
    assert is_remote_locator(value)
    assert is_addressed(value)


@pytest.mark.parametrize("value", [
    "node://hpc/cells",
    "/data/slide.ome.tif",
    r"C:\data\slide.zarr",
    "relative/x.zarr",
    "https:/host/x.zarr",  # what Path() makes of one -- not an address
    "ftp://host/x.zarr",
    "",
    None,
])
def test_other_values_are_not(value):
    assert not is_remote_locator(value)


def test_node_locators_are_addressed_but_not_remote():
    assert is_addressed("node://hpc/cells")
    assert is_node_locator("node://hpc/cells")
    assert not is_remote_locator("node://hpc/cells")


@pytest.mark.parametrize("raw, expected", [
    ("HTTPS://UK1S3.Embassy.EBI.ac.uk/idr/X.zarr/", "https://uk1s3.embassy.ebi.ac.uk/idr/X.zarr"),
    ('"https://host/a//b.zarr"', "https://host/a/b.zarr"),
    ("gcs://bucket/x.zarr", "gs://bucket/x.zarr"),
    ("abfss://c/x.zarr", "abfs://c/x.zarr"),
    ("https://host/x.zarr#frag", "https://host/x.zarr"),
    ("https://host/x.zarr?X-Amz-Signature=abc", "https://host/x.zarr?X-Amz-Signature=abc"),
])
def test_canonical_url(raw, expected):
    assert remote_store.canonical_url(raw) == expected


@pytest.mark.parametrize("url, root, sub", [
    ("https://h/a/x.zarr", "https://h/a/x.zarr", ""),
    ("https://h/a/x.zarr/0", "https://h/a/x.zarr", "0"),
    ("https://h/a/plate.ome.zarr/B/2/0", "https://h/a/plate.ome.zarr", "B/2/0"),
    # The LAST .zarr segment: a store inside a folder named like one.
    ("s3://b/outer.zarr/inner.zarr/images/x", "s3://b/outer.zarr/inner.zarr", "images/x"),
    ("https://h/no-suffix/0", "https://h/no-suffix/0", ""),
])
def test_split_store_url(url, root, sub):
    assert remote_store.split_store_url(url) == (root, sub)


def test_names():
    url = "https://h/idr/BR00109990_C2.ome.zarr/0?sig=1"
    assert remote_store.url_name(url) == "0"
    assert remote_store.display_name("https://h/x/human.h5ad.zarr") == "human"
    assert remote_store.url_join("https://h/x.zarr?sig=1", "labels", "cells") == \
        "https://h/x.zarr/labels/cells?sig=1"


def test_picked_paths_keep_web_addresses_whole():
    from plexora.server.routes.import_routes import _picked

    picked = _picked({"paths": ["  'HTTPS://Host/a//x.zarr/'  ", "", "node://hpc/r"]})
    assert picked == ["https://host/a/x.zarr", "", "node://hpc/r"]


def test_dataset_name_from_a_web_address():
    from plexora.datasource import _derive_dataset_name_from_path

    assert _derive_dataset_name_from_path(
        "https://h/idr/BR00109990_C2.ome.zarr") == "BR00109990_C2"


def test_existing_project_found_by_canonical_url():
    from plexora.datasource import _find_existing_datasource_for_image

    config = {"a": {"channelFile": "/data/local.ome.tif"},
              "b": {"channelFile": "https://host/x.zarr/0"}}
    assert _find_existing_datasource_for_image("HTTPS://HOST/x.zarr/0/", config) == "b"
    assert _find_existing_datasource_for_image("https://host/x.zarr/1", config) is None


def test_a_web_address_is_never_copied():
    from pathlib import Path

    from plexora.datasource import _copy_if_requested

    assert _copy_if_requested("https://h/x.zarr", Path("."), False) == "https://h/x.zarr"
    with pytest.raises(ValueError, match="Make available offline"):
        _copy_if_requested("https://h/x.zarr", Path("."), True)


def test_format_sniffers_never_treat_an_address_as_a_file():
    from plexora.server.utils import dicom_wsi, xenium_focus

    assert not dicom_wsi.is_dicom_path("https://h/slide.dcm")
    assert not xenium_focus.is_focus_dir("https://h/morphology_focus")


# -- the address book ------------------------------------------------------


def test_address_book_longest_prefix_wins(plexora_data_root):
    from plexora.server.models import remote_sources

    remote_sources.set_options("s3://idr", {"endpoint_url": "https://a.example", "anon": True})
    remote_sources.set_options("s3://idr/private/", {"anon": False, "profile": "lab"})
    assert remote_sources.options_for("s3://idr/zarr/x.zarr")["endpoint_url"] == "https://a.example"
    assert remote_sources.options_for("s3://idr/private/y.zarr") == {"anon": False, "profile": "lab"}
    # A prefix is a directory: `s3://idr/` must not match `s3://idr-backup/`.
    assert remote_sources.options_for("s3://idr-backup/x.zarr") == {}
    # The bucket root itself matches its own prefix.
    assert remote_sources.options_for("s3://idr")["endpoint_url"] == "https://a.example"


def test_address_book_never_stores_a_secret(plexora_data_root):
    import json

    from plexora.server.models import remote_sources

    remote_sources.set_options("s3://lab/", {
        "anon": "false", "profile": "lab", "aws_secret_access_key": "SHHH",
        "key": "AKIA", "secret": "SHHH", "token": "T"})
    text = remote_sources.book_path().read_text()
    assert "SHHH" not in text and "AKIA" not in text
    assert json.loads(text)["sources"]["s3://lab/"] == {"anon": False, "profile": "lab"}


def test_address_book_rejects_non_addresses(plexora_data_root):
    from plexora.server.models import remote_sources

    with pytest.raises(ValueError):
        remote_sources.set_options("/local/path", {})
    with pytest.raises(ValueError):
        remote_sources.set_options("s3://b/", {"endpoint_url": "not a url"})
    assert remote_sources.delete_options("s3://nothing/") is False


def test_storage_options_per_scheme():
    s3 = remote_store.storage_options_for(
        "s3://idr/x.zarr", {"endpoint_url": "https://e", "anon": True, "profile": "p"})
    assert s3["anon"] is True
    assert s3["client_kwargs"] == {"endpoint_url": "https://e"}
    # A profile means nothing to an anonymous client and is not passed.
    assert "profile" not in s3
    private = remote_store.storage_options_for("s3://b/x.zarr", {"anon": False, "profile": "p"})
    assert private["anon"] is False and private["profile"] == "p"
    https = remote_store.storage_options_for("https://h/x.zarr")
    assert https["client_kwargs"]["trust_env"] is True
    assert remote_store.storage_options_for("gs://b/x.zarr") == {"token": "anon"}


def test_a_missing_scheme_package_names_the_extra(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "gcsfs" else real(name, *a))
    with pytest.raises(remote_store.RemoteSupportMissing) as caught:
        remote_store.open_store("gs://bucket/x.zarr")
    assert "plexora[remote]" in str(caught.value)
    assert caught.value.INSTALL == "pip install 'plexora[remote]'"
