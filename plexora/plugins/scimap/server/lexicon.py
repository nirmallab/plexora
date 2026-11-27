"""The notebook's words for each kind of data.

A multiplexed image has markers, intensities and cells; a Xenium run has
genes, counts and cells; a Visium run has genes, counts and spots. Tool names,
summaries, field labels, chips and view titles are written once with
placeholders and filled here for the notebook's mode (`facts.mode`), on the
server, so every surface says the same word:

    {feature} {features} {Feature} {Features}   marker / gene
    {unit} {units} {Unit} {Units}               cell / spot / bin
    {values} {Values}                           intensities / counts
    {identity} {Identity}                       cell type / cluster
    {sample} {Sample}                           image / sample

An unknown mode reads as imaging, which is what the notebook said before it
knew any other kind of data. A brace that names no word is left as written.
"""

from __future__ import annotations

import re

WORDS = {
    "imaging": {"feature": "marker", "features": "markers", "unit": "cell", "units": "cells",
                "values": "intensities", "identity": "cell type", "sample": "image"},
    "st-cell": {"feature": "gene", "features": "genes", "unit": "cell", "units": "cells",
                "values": "counts", "identity": "cell type", "sample": "sample"},
    "st-spot": {"feature": "gene", "features": "genes", "unit": "spot", "units": "spots",
                "values": "counts", "identity": "cluster", "sample": "sample"},
    "st-bin": {"feature": "gene", "features": "genes", "unit": "bin", "units": "bins",
               "values": "counts", "identity": "cluster", "sample": "sample"},
}
DEFAULT = "imaging"
_SLOT = re.compile(r"\{(\w+)\}")


def words(mode=None) -> dict:
    """Every word for `mode`, lower-case and capitalised (`feature`, `Feature`)."""
    base = WORDS.get(mode or DEFAULT, WORDS[DEFAULT])
    return {**base, **{key[:1].upper() + key[1:]: value[:1].upper() + value[1:] for key, value in base.items()}}


def fill(text, mode=None):
    """`text` with each `{word}` filled for `mode`; anything that is not text is returned as it is."""
    if not isinstance(text, str) or "{" not in text:
        return text
    table = words(mode)
    return _SLOT.sub(lambda m: table.get(m.group(1), m.group(0)), text)


def fill_all(value, mode=None):
    """`fill` through dicts and lists: a tool's whole description in one call."""
    if isinstance(value, dict):
        return {key: fill_all(item, mode) for key, item in value.items()}
    if isinstance(value, list):
        return [fill_all(item, mode) for item in value]
    if isinstance(value, tuple):
        return tuple(fill_all(item, mode) for item in value)
    return fill(value, mode)


__all__ = ["DEFAULT", "WORDS", "fill", "fill_all", "words"]
