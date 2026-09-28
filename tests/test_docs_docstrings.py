"""Every documented symbol's docstring follows the Google convention.

One test per page. Diagnostics (see tools/docs/docstrings.py DIAGNOSTICS)
must be empty once manifest.yaml `docstrings.waivers` are applied. Symbols
in `docstrings.backlog` are expected to fail (xfail strict): fixing one makes
its test XPASS, which fails until the name is taken off the backlog.
"""

import pytest

from tools.docs import docstrings, manifest, python_pages
from tools.docs.python_api import build_model

MANIFEST = manifest.load()
BACKLOG = set(MANIFEST.docstrings.get("backlog") or [])


@pytest.fixture(scope="module")
def symbols():
    return build_model()["symbols"]


def _params():
    for qualname in sorted(MANIFEST.pages):
        marks = [pytest.mark.xfail(strict=True, reason="on the docstring backlog in manifest.yaml")] \
            if qualname in BACKLOG else []
        yield pytest.param(qualname, marks=marks, id=qualname)


@pytest.mark.parametrize("qualname", list(_params()))
def test_docstring_is_complete(qualname, symbols):
    found = python_pages.diagnose(symbols[qualname], MANIFEST, symbols)
    remaining, _stale = python_pages.apply_waivers(qualname, found, MANIFEST)
    lines = [f"{code}: {detail}  ({docstrings.DIAGNOSTICS[code]})" for code, detail in remaining]
    assert not remaining, f"{qualname}:\n  " + "\n  ".join(lines)


def test_no_stale_waivers(symbols):
    stale = []
    for qualname in sorted(MANIFEST.pages):
        found = python_pages.diagnose(symbols[qualname], MANIFEST, symbols)
        _remaining, unused = python_pages.apply_waivers(qualname, found, MANIFEST)
        stale += [f"{qualname}: {w['code']} {w.get('param', '')}".strip() for w in unused]
    assert not stale, "waivers for problems that no longer occur:\n  " + "\n  ".join(stale)


def test_every_diagnostic_code_is_described():
    """Every code the parser or the page checks emit has an explanation."""
    import inspect
    import re

    from tools.docs import python_api

    source = "".join(inspect.getsource(m) for m in (docstrings, python_pages, python_api))
    emitted = set(re.findall(r'\(\s*"([a-z]+(?:_[a-z]+)+)",', source))
    assert emitted, "no diagnostic codes found; has the emitting code changed shape?"
    assert emitted <= set(docstrings.DIAGNOSTICS), emitted - set(docstrings.DIAGNOSTICS)
