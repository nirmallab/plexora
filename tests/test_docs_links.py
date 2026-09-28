"""Where the app sends people for documentation, and that it exists.

plexora/links.py `DOCS_URL` is the one address; the desktop shell's copy,
the site's own configuration and the browser's fallback must match it, and
every plugin's `help.docs` slug must be a page the site actually has.
"""

import re
from pathlib import Path

from plexora import links

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "website" / "content" / "docs"


def test_docs_url_is_one_address():
    assert links.DOCS_URL.endswith("/")
    menu = (ROOT / "desktop" / "src-tauri" / "src" / "menu.rs").read_text(encoding="utf-8")
    assert f'const DOCS_URL: &str = "{links.DOCS_URL}";' in menu
    help_menu = (ROOT / "plexora" / "client" / "src" / "js" / "views" / "helpMenu.js").read_text(encoding="utf-8")
    assert f'const DEFAULT_DOCS_URL = "{links.DOCS_URL}";' in help_menu
    site = (ROOT / "website" / "lib" / "site.ts").read_text(encoding="utf-8")
    match = re.search(r"siteUrl = process\.env\.NEXT_PUBLIC_SITE_URL \?\? '([^']+)'", site)
    assert match and match.group(1).rstrip("/") + "/" == links.DOCS_URL


def test_the_page_carries_the_docs_url():
    import plexora

    client = plexora.app.test_client()
    response = client.get("/")
    body = response.get_data(as_text=True)
    assert f'data-plexora-docs-url="{links.DOCS_URL}"' in body


def _help_slugs():
    for path in sorted((ROOT / "plexora").rglob("*.js")):
        if "node_modules" in path.parts or "dist" in path.parts:
            continue
        for slug in re.findall(r'\bdocs:\s*"([^"]+)"', path.read_text(encoding="utf-8")):
            yield path.relative_to(ROOT).as_posix(), slug


def test_every_plugin_help_slug_is_a_page():
    slugs = list(_help_slugs())
    assert len(slugs) >= 6, slugs
    for source, slug in slugs:
        page = CONTENT / f"{slug}.mdx"
        index = CONTENT / slug / "index.mdx"
        assert page.exists() or index.exists(), f"{source}: help.docs '{slug}' has no page under website/content/docs"


def test_updater_uses_the_same_repository():
    from plexora import updates

    assert updates.GITHUB_REPO == links.GITHUB_REPO
