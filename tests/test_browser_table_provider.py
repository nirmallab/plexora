"""A table the website's page holds: read from the page, normalised as a file.

On the website the page's column store parses the user's CSV (polars in
WebAssembly cannot), and Python takes its columns from there
(plexora/server/providers/browser.py). These pin the two promises that make
that safe: the table that comes out is the one `LocalTableProvider` makes from
the same file, and outside a browser such a project says what it is rather than
reading something else.
"""

import dataclasses

import polars as pl
import pytest

from plexora.server import providers
from plexora.server.models.project import Project, ResourceBinding
from plexora.server.providers import browser
from plexora.server.providers.base import ResourceNotLocal
from plexora.server.providers.local import LocalTableProvider
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def project(tmp_path):
    made = make_synthetic_project(tmp_path, "pagetable")
    found = Project.load(made["name"])
    bound = dataclasses.replace(found, resources={
        **dict(found.resources or {}),
        "table": ResourceBinding(kind="table", provider="browser")})
    yield bound, made
    browser.set_column_source(None)


def test_a_browser_bound_table_is_served_by_the_browser_provider(project):
    bound, _made = project
    table = providers.resolve_providers(bound).table
    assert isinstance(table, browser.BrowserTableProvider)
    assert table.locator.provider == "browser"


def test_an_unbound_table_is_still_local(project):
    _bound, made = project
    table = providers.resolve_providers(Project.load(made["name"])).table
    assert type(table) is LocalTableProvider


def test_outside_a_browser_it_says_where_the_rows_are(project):
    bound, _made = project
    with pytest.raises(ResourceNotLocal, match="Plexora website"):
        providers.resolve_providers(bound).table.load()


def test_the_pages_columns_make_the_table_the_file_makes(project):
    bound, made = project
    asked = []

    def from_the_page(spec):
        asked.append(spec.src)
        return pl.read_csv(made["csv_path"])  # what the column store hands over

    browser.set_column_source(from_the_page)
    from_page = providers.resolve_providers(bound).table.load()
    from_file = LocalTableProvider(bound.dataset, bound.name).load()
    assert asked == [bound.dataset.src]
    assert from_page.table.equals(from_file.table)
    assert from_page.table.columns == from_file.table.columns


def test_a_page_without_a_copy_reads_the_mounted_file(project):
    bound, _made = project
    browser.set_column_source(lambda spec: None)
    loaded = providers.resolve_providers(bound).table.load()
    assert loaded.table.height > 0


def test_on_the_website_every_reader_of_a_picked_csv_takes_the_pages_parse(project, monkeypatch):
    """Not only the bound provider: an agent session's own LocalTableProvider
    of a picked CSV reads through the page too (polars there panics on a large
    file, and stays broken after)."""
    _bound, made = project
    record = Project.load(made["name"])
    asked = []

    def from_the_page(spec):
        asked.append(spec.src)
        return pl.read_csv(made["csv_path"])

    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    monkeypatch.setattr(browser, "picked", lambda spec: True)
    browser.set_column_source(from_the_page)
    first = LocalTableProvider(record.dataset, record.name).load()
    second = LocalTableProvider(record.dataset, record.name).load()
    expected = LocalTableProvider(record.dataset, record.name)
    monkeypatch.delenv("PLEXORA_BACKEND")
    assert asked == [record.dataset.src] * 2
    assert first.table.equals(expected.load().table) and second.table.equals(first.table)


def test_off_the_website_a_csv_is_never_the_pages(project, monkeypatch):
    _bound, made = project
    record = Project.load(made["name"])
    browser.set_column_source(lambda spec: pytest.fail("asked the page"))
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    assert not browser.picked(record.dataset)
    LocalTableProvider(record.dataset, record.name).load()
