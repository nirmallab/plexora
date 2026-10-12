"""What Plexora writes to the terminal, decided in one place.

Plexora's modules log through `logging.getLogger(__name__)` and never print
progress: "Loading segmentation.", "Creating KD Tree." and the like are
INFO and DEBUG records that nobody sees until they ask. The entry points that
own a terminal (`plexora`, the desktop server, the notebook sidecar, a data
node) call `configure()` once, before serving, and that call is the whole
policy:

- one handler, on the root logger, writing to stderr. Never stdout: the desktop
  app reads exactly one JSON line from it, `plexora connect` parses a node's
  announce from it, and MCP speaks JSON-RPC over it.
- Plexora's own loggers at WARNING unless `-v` (DEBUG) or PLEXORA_LOG_LEVEL
  says otherwise; every other library's loggers stay at WARNING, so `-v` is
  Plexora's chatter and not Pillow's.
- on the root rather than on the `plexora` logger, so that waitress's
  `logging.basicConfig()` inside `serve()` finds a handler and adds none, and
  Flask's `default_handler` on the app logger (also named "plexora") is taken
  off so a line is never written twice.

Library use (a notebook importing plexora) configures nothing: records below
WARNING go nowhere and warnings reach Python's last-resort handler on stderr,
which is what a library owes its host.
"""

from __future__ import annotations

import logging
import os
import sys

LOGGER_NAME = "plexora"
LEVEL_ENV = "PLEXORA_LOG_LEVEL"

TERMINAL_FORMAT = "%(levelname)s: %(message)s"
VERBOSE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
VERBOSE_DATEFMT = "%H:%M:%S"
# Every line of the desktop server lands in a log file read only after
# something went wrong, so it carries a full date and the logger's name.
DESKTOP_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_HANDLER_TAG = "_plexora_handler"


def resolve_level(verbose=False, env=None, default=logging.WARNING):
    """The level Plexora's loggers run at.

    `verbose` wins (DEBUG). Otherwise PLEXORA_LOG_LEVEL (or `env`, when given)
    as a level name in any case (`info`, `WARN`) or a number; anything
    unrecognisable falls back to `default` instead of stopping a server over a
    typo in an environment variable.
    """
    if verbose:
        return logging.DEBUG
    raw = (os.environ.get(LEVEL_ENV) if env is None else env) or ""
    raw = raw.strip()
    if not raw:
        return default
    if raw.isdigit():
        return int(raw)
    level = logging.getLevelName(raw.upper())
    return level if isinstance(level, int) else default


def _our_handler(root):
    for handler in root.handlers:
        if getattr(handler, _HANDLER_TAG, False):
            return handler
    return None


def configure(level=None, *, verbose=False, fmt=None, everything=False):
    """Send log records to stderr at the resolved level; return that level.

    Idempotent: a second call reuses its handler and only changes the level
    and format. `fmt` defaults to the short terminal form, or the timestamped
    one at DEBUG. `everything` applies the level to every library's loggers as
    well, which is what the desktop server's log file has always had.
    """
    resolved = resolve_level(verbose) if level is None else level
    if fmt is None:
        fmt = VERBOSE_FORMAT if resolved <= logging.DEBUG else TERMINAL_FORMAT
    datefmt = VERBOSE_DATEFMT if fmt == VERBOSE_FORMAT else None

    root = logging.getLogger()
    handler = _our_handler(root)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    handler.setFormatter(logging.Formatter(fmt, datefmt))
    root.setLevel(resolved if everything else logging.WARNING)
    # Records from "plexora.*" propagate to the root's handler whatever the
    # root's own level is; only the logger they start from filters them.
    ours = logging.getLogger(LOGGER_NAME)
    ours.setLevel(resolved)
    try:
        from flask.logging import default_handler
    except ImportError:  # pragma: no cover - flask is a hard dependency
        pass
    else:
        ours.removeHandler(default_handler)
    return resolved
