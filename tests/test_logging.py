"""What reaches the terminal (plexora/_logging.py).

Load-time chatter ("Loading segmentation.", the spatial index) is INFO and
DEBUG, warnings stay visible, and nothing is ever written to stdout -- the
desktop app reads one JSON line from it and a node's announce is parsed off it.
"""

import logging

import pytest

from plexora import _logging


@pytest.fixture
def clean_logging():
    root = logging.getLogger()
    ours = logging.getLogger(_logging.LOGGER_NAME)
    saved = (list(root.handlers), root.level, list(ours.handlers), ours.level)
    yield
    for handler in list(root.handlers):
        if handler not in saved[0]:
            root.removeHandler(handler)
    root.setLevel(saved[1])
    for handler in saved[2]:
        if handler not in ours.handlers:
            ours.addHandler(handler)
    ours.setLevel(saved[3])


@pytest.mark.parametrize("raw, expected", [
    (None, logging.WARNING), ("", logging.WARNING), ("info", logging.INFO),
    ("DEBUG", logging.DEBUG), (" warn ", logging.WARNING), ("10", 10),
    ("loud", logging.WARNING)])
def test_the_level_comes_from_the_environment_and_a_typo_is_ignored(raw, expected,
                                                                      monkeypatch):
    if raw is None:
        monkeypatch.delenv(_logging.LEVEL_ENV, raising=False)
    else:
        monkeypatch.setenv(_logging.LEVEL_ENV, raw)
    assert _logging.resolve_level() == expected


def test_verbose_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(_logging.LEVEL_ENV, "ERROR")
    assert _logging.resolve_level(verbose=True) == logging.DEBUG


def _our_handlers():
    return [h for h in logging.getLogger().handlers
            if getattr(h, _logging._HANDLER_TAG, False)]


def test_by_default_chatter_is_quiet_and_warnings_reach_stderr(clean_logging, capsys,
                                                                monkeypatch):
    monkeypatch.delenv(_logging.LEVEL_ENV, raising=False)
    assert _logging.configure() == logging.WARNING
    log = logging.getLogger("plexora.server.models.data_model")
    log.info("Loading segmentation.")
    log.warning("plugin %r failed to load", "broken")
    out, err = capsys.readouterr()
    assert out == ""
    assert "Loading segmentation." not in err
    assert "WARNING: plugin 'broken' failed to load" in err


def test_verbose_shows_plexora_and_not_other_libraries(clean_logging, capsys):
    _logging.configure(verbose=True)
    logging.getLogger("plexora.server.models.data_model").debug("Spatial index built")
    logging.getLogger("PIL.PngImagePlugin").info("STREAM b'IHDR'")
    out, err = capsys.readouterr()
    assert out == ""
    assert "plexora.server.models.data_model: Spatial index built" in err
    assert "IHDR" not in err


def test_configuring_twice_keeps_one_handler_and_takes_flask_s_off(clean_logging, capsys):
    from flask.logging import default_handler

    logging.getLogger(_logging.LOGGER_NAME).addHandler(default_handler)
    _logging.configure(logging.INFO)
    _logging.configure(logging.INFO)
    assert len(_our_handlers()) == 1
    assert default_handler not in logging.getLogger(_logging.LOGGER_NAME).handlers
    logging.getLogger("plexora.x").info("once")
    _out, err = capsys.readouterr()
    assert err.count("once") == 1


def test_everything_applies_the_level_to_every_library(clean_logging, capsys):
    _logging.configure(logging.INFO, fmt=_logging.DESKTOP_FORMAT, everything=True)
    logging.getLogger("waitress").info("Serving on http://127.0.0.1")
    _out, err = capsys.readouterr()
    assert "INFO waitress: Serving on" in err
