"""The three things "base URL" can mean, kept from being confused again.

`clean_prefix` and `prefix_with_slash` differ by one character and used to be
the same function under the same name in two modules. This file pins that they
disagree on purpose, and that the third case -- a full origin, which only
Colab produces -- is rejected rather than silently mangled into a path.
"""

import importlib.util
from pathlib import Path

import pytest

from plexora._url import clean_prefix, is_full_origin, join_display, prefix_with_slash


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("/", ""),
        ("/user/me", "/user/me"),
        ("user/me", "/user/me"),
        ("/user/me/", "/user/me"),
        ("  /user/me/  ", "/user/me"),
        ("/proxy/8000", "/proxy/8000"),
    ],
)
def test_clean_prefix_never_leaves_a_trailing_slash(value, expected):
    assert clean_prefix(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/", "/"),
        ("/user/me", "/user/me/"),
        ("user/me", "/user/me/"),
        ("/user/me/", "/user/me/"),
    ],
)
def test_prefix_with_slash_always_leaves_one(value, expected):
    assert prefix_with_slash(value) == expected


def test_the_two_prefix_forms_disagree_about_no_prefix():
    """The whole reason both exist. "" concatenates cleanly in front of a path
    that starts with "/"; "/" concatenates cleanly in front of one that does
    not, which is the form jupyter.py builds `proxy/<port>` onto."""
    assert clean_prefix("") == ""
    assert prefix_with_slash("") == "/"


@pytest.mark.parametrize(
    "value",
    ["http://localhost:8000", "https://abc123-8000.googleusercontent.com", "HTTPS://X"],
)
def test_full_origins_are_recognised(value):
    assert is_full_origin(value)


@pytest.mark.parametrize("value", ["", None, "/user/me", "proxy/8000"])
def test_mount_paths_are_not_origins(value):
    assert not is_full_origin(value)


def test_clean_prefix_refuses_a_full_origin():
    """The Colab corruption class, caught at the boundary.

    Without this the origin came back as "/https:/abc.googleusercontent.com"
    -- a valid-looking path that fails much later and nowhere near the mistake.
    """
    with pytest.raises(ValueError) as excinfo:
        clean_prefix("https://abc123-8000.googleusercontent.com")
    assert "abc123-8000.googleusercontent.com" in str(excinfo.value)


def test_prefix_with_slash_refuses_one_too():
    with pytest.raises(ValueError):
        prefix_with_slash("https://abc123-8000.googleusercontent.com")


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("", "/tonsil"),
        ("/user/me/proxy/8123", "/user/me/proxy/8123/tonsil"),
        ("/user/me/proxy/8123/", "/user/me/proxy/8123/tonsil"),
        ("http://127.0.0.1:8123", "http://127.0.0.1:8123/tonsil"),
        ("https://abc.googleusercontent.com", "https://abc.googleusercontent.com/tonsil"),
    ],
)
def test_join_display_handles_all_three_kinds_of_base(base, expected):
    assert join_display(base, "tonsil") == expected


def test_join_display_with_no_segments_is_the_base_root():
    assert join_display("") == "/"
    assert join_display("/user/me/proxy/8123") == "/user/me/proxy/8123/"
    assert join_display("http://127.0.0.1:8123") == "http://127.0.0.1:8123/"


def test_join_display_quotes_segments():
    """A project name is user-typed and routinely has spaces and brackets in
    it; the URL has to survive being pasted somewhere."""
    assert join_display("", "Tonsil 2 (repeat)") == "/Tonsil%202%20%28repeat%29"


def test_join_display_ignores_empty_segments():
    assert join_display("/base", None, "", "x") == "/base/x"


def _load_standalone(relative):
    spec = importlib.util.spec_from_file_location(
        f"standalone_{Path(relative).stem}", ROOT / relative
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cli_copy_of_clean_prefix_still_agrees_with_the_original():
    """cli.py may not import the plexora package -- it is loaded off disk by
    tests/test_cli.py and frozen into a onefile executable where an importlib
    file loader could not reach the package. So it keeps a copy, and this is
    what stops the copy drifting."""
    cli = _load_standalone("plexora/cli.py")
    for value in (None, "", "/", "/user/me", "user/me", "/user/me/", "  /a/b/  "):
        assert cli._clean_base_url(value) == clean_prefix(value), value
