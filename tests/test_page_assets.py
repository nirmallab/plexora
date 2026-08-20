"""Every stylesheet and script a page ships has to actually reach the browser.

The templates used to reference assets relatively (`../client/src/...`), which
resolves against the URL the *page* was served at. That works only for a page
exactly one segment deep at the site root, and silently produces a 404 URL
otherwise -- no error anywhere on the server, just a page with no CSS and no JS.

It bit twice. `/project/<name>/columns` is three segments deep, so the Confirm
Columns screen rendered as unstyled HTML with an empty column list and a
Continue button that did nothing: the classifier script that fills it in had
404'd. And under a mounted deployment (the Jupyter sidecar sets
PLEXORA_BASE_URL) the extra prefix pushed *every* page off by one, so all of
them loaded bare.

These tests resolve each reference the way a browser would and fetch it, so a
new page at a new depth cannot reintroduce either.
"""

import json
import re
from pathlib import Path
from urllib.parse import urljoin

import pytest

import plexora
from tests.helpers import ALL_CONFIRMED, csv_spec, project

TEMPLATES = Path(plexora.__file__).parent / "client" / "templates"

#: Every page core renders, one per template, with the URL depth it is served
#: at -- which is the whole point: the bug was invisible at depth 1.
PAGES = (
    "/",
    "/upload_page",
    "/open_project",
    "/demo",
    "/edit_config/demo",
    "/project/demo/columns",
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(plexora, "data_path", tmp_path)
    monkeypatch.setattr(plexora, "config_json_path", tmp_path / "config.json")
    csv_path = tmp_path / "cells.csv"
    csv_path.write_text("CellID,X_centroid,Y_centroid,CD3\n1,0,0,5\n", encoding="utf-8")
    record = project(
        "demo",
        dataset=csv_spec(csv_path, markers=["CD3"],
                         metadata=["CellID", "X_centroid", "Y_centroid"]),
        confirmed=ALL_CONFIRMED,
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"demo": record.to_entry()}), encoding="utf-8")
    return plexora.app.test_client()


def _asset_refs(html):
    return [url for url in re.findall(r'(?:src|href)="([^"]+)"', html)
            if "client/" in url]


def _broken_refs(client, page, prefix=""):
    """Fetch every asset the page names, resolved from the URL a browser asked
    for. Returns the ones that do not come back 200."""
    response = client.get(page)
    assert response.status_code == 200, f"{page} returned {response.status_code}"
    refs = _asset_refs(response.get_data(as_text=True))
    assert refs, f"{page} references no assets at all -- did the template change?"

    broken = []
    for ref in refs:
        # A relative href resolves against the full URL the browser requested,
        # mount prefix included.
        resolved = urljoin("http://host" + prefix + page, ref)[len("http://host"):]
        # The proxy in front strips the prefix back off before the app sees it;
        # a URL that never had the prefix arrives unchanged, and wrong.
        served = resolved[len(prefix):] if prefix and resolved.startswith(prefix) else resolved
        if client.get(served).status_code != 200:
            broken.append(f"{ref} -> {resolved}")
    return broken


@pytest.mark.parametrize("page", PAGES)
def test_every_asset_a_page_names_can_be_fetched(client, page):
    assert not _broken_refs(client, page), (
        f"{page} names assets that 404. The page renders with no styling and "
        f"none of its behaviour: {_broken_refs(client, page)}"
    )


@pytest.mark.parametrize("page", PAGES)
def test_assets_still_resolve_under_a_mount_prefix(client, page, monkeypatch):
    """The Jupyter sidecar serves the whole app under /proxy/<port>. Asset URLs
    have to carry that prefix, which is why they are built from base_url rather
    than written relative to whatever page happens to reference them."""
    prefix = "/proxy/8000"
    monkeypatch.setitem(plexora.app.config, "PLEXORA_BASE_URL", prefix)

    assert not _broken_refs(client, page, prefix=prefix)


def test_no_template_goes_back_to_relative_asset_urls():
    """The rule itself, stated once. Catching it here names the fix; catching it
    only through a 404 above leaves the next person guessing."""
    offenders = {}
    for template in TEMPLATES.glob("*.html"):
        relative = re.findall(r'(?:src|href)="(\.\./[^"]*)"', template.read_text(encoding="utf-8"))
        if relative:
            offenders[template.name] = relative
    assert not offenders, (
        f"these templates reference assets relative to the page URL: {offenders}. "
        "Use {{ data.base_url }}/client/... -- a relative URL resolves against "
        "the page's own path, so it breaks on any page at a different depth."
    )
