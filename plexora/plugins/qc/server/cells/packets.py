"""Cell-QC packets (filled in with the modules)."""

from __future__ import annotations


def build(engine, units):
    for unit in units:
        engine.close(unit, "skipped_not_applicable", "cell QC is not available yet")
    return None


def apply(engine, packet, answer):
    raise NotImplementedError
