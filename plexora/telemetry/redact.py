"""The second line of defence: look at a built batch for anything that should
never be in one, and throw out the event that carries it.

The schema is the first line and should make this module redundant: no field
can hold free text. This exists for the day somebody adds a field with too
loose a pattern, and it checks for what would matter most if that happened --
a path, a URL, an address, a token, a long hex run that is not one of our own
ids, and the user's own names (login, host, home and data directories, and the
projects they have registered) appearing as a value.

The names are gathered on the uploader thread when a batch is built and are
never stored or sent.
"""

from __future__ import annotations

import getpass
import os
import re
import socket
from pathlib import Path

_FORBIDDEN_CHARS = re.compile(r"[/\\@?=\s\"'`{}|%&;#~$]")
_URLISH = re.compile(r"https?|ftp:|file:|www\.", re.IGNORECASE)
_LONG_HEX = re.compile(r"[0-9a-fA-F]{32,}")
_ID_KEYS = frozenset({"install_id", "session_id", "batch_id"})


_VOCAB = None


def _vocabulary() -> frozenset:
    """Every word a closed vocabulary in the schema can produce."""
    global _VOCAB
    if _VOCAB is None:
        from plexora.telemetry import schema

        words = set(schema.FIRST_PARTY) | set(schema.EVENTS) | set(schema.MODES)

        def walk(type_):
            if isinstance(type_, schema.Enum):
                words.update(type_.values)
            for child in ("of", "key", "value"):
                if hasattr(type_, child):
                    walk(getattr(type_, child))
            for sub in getattr(type_, "fields", {}).values():
                walk(sub)

        for spec in schema.EVENTS.values():
            if isinstance(spec, schema.Record):
                words.update(spec.props)
                for field in spec.props.values():
                    walk(field.type)
            else:
                words.update(spec.keys)
                for key in spec.keys.values():
                    words.update(key.dims)
                    for field in key.dims.values():
                        walk(field.type)
        words.update(schema.CLIENT_BLOCK)
        for field in schema.CLIENT_BLOCK.values():
            walk(field.type)
        _VOCAB = frozenset(words)
    return _VOCAB


class Dirty(ValueError):
    """A string in a batch that must not leave the machine."""


def personal_names() -> set:
    """Lower-cased names of this user and machine that must never be a value."""
    names = set()

    def add(value, minimum=3):
        text = str(value or "").strip().lower()
        if len(text) >= minimum:
            names.add(text)

    for getter in (getpass.getuser, os.getlogin):
        try:
            add(getter())
        except Exception:
            pass
    try:
        host = socket.gethostname()
        add(host)
        add(host.split(".")[0])
    except Exception:
        pass
    try:
        add(Path.home().name)
    except Exception:
        pass
    try:
        from plexora import paths

        add(paths.data_root().name)
    except Exception:
        pass
    try:
        from plexora.server.models.project import Project

        for name in Project.load_all():
            add(name, 4)
    except Exception:
        pass
    return names


def check_string(value: str, names=frozenset(), *, id_key=False) -> None:
    if _FORBIDDEN_CHARS.search(value):
        raise Dirty("a path, address or query character")
    if _URLISH.search(value):
        raise Dirty("a URL")
    if not id_key and _LONG_HEX.search(value):
        raise Dirty("a long hex run")
    lowered = value.lower()
    if lowered in names and value not in _vocabulary():
        # A login that happens to be `data` is not leaked by a `family=data`
        # dimension every install sends: closed-vocabulary words are exempt.
        raise Dirty("a personal name")


def assert_clean(obj, names=frozenset(), _key=None) -> None:
    """Raise `Dirty` if any key or string in `obj` looks personal."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise Dirty("a non-string key")
            check_string(key, names)
            assert_clean(value, names, key)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            assert_clean(value, names, _key)
    elif isinstance(obj, str):
        check_string(obj, names, id_key=_key in _ID_KEYS)
    elif obj is None or isinstance(obj, (bool, int, float)):
        return
    else:
        raise Dirty(f"a {type(obj).__name__}")


def clean_events(events: list, names=frozenset()) -> tuple[list, int]:
    """`(kept, dropped)`: the events that pass `assert_clean`."""
    kept, dropped = [], 0
    for event in events:
        try:
            assert_clean(event, names)
        except Dirty:
            dropped += 1
            continue
        kept.append(event)
    return kept, dropped
