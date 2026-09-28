"""The signature a generated page states is the live object's signature.

Line numbers drifting is a freshness failure; a signature drifting is worse
-- the page is wrong -- so it fails here even when nobody has regenerated.
"""

import json
from pathlib import Path

import pytest

from tools.docs import config
from tools.docs.python_api import build_model


def generated_pages():
    for base in ("python-api", "plugin-api/descriptor", "plugin-api/runtime"):
        for path in sorted((config.CONTENT / base).rglob("*.mdx")):
            head = path.read_text(encoding="utf-8").split("---", 2)[1]
            fields = {}
            for line in head.splitlines():
                if ":" in line and not line.startswith(" "):
                    key, _, value = line.partition(":")
                    if value.strip():
                        fields[key.strip()] = json.loads(value)
            if "symbol" in fields:
                yield pytest.param(fields["symbol"], fields.get("signature"), id=fields["symbol"])


@pytest.fixture(scope="module")
def symbols():
    return build_model()["symbols"]


@pytest.mark.parametrize("qualname,signature", list(generated_pages()))
def test_page_signature_matches_the_code(qualname, signature, symbols):
    assert qualname in symbols, f"{qualname} has a page but is no longer public"
    assert signature == symbols[qualname]["signature"]
