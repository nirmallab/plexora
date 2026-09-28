"""Where Plexora points people: the repository, the issue tracker, PyPI and
the documentation site.

One module so the browser (Help menu, a plugin's help button), the updater and
the desktop shell cannot drift apart. `base.html` renders `DOCS_URL` into the
page for the client, and tests/test_docs_links.py holds the desktop menu's
copy in `desktop/src-tauri/src/menu.rs` to this value.

Moving the documentation to plexoraapp.com is a change to `DOCS_URL` here and
to the site's own base path (website/lib/site.ts), nothing else.
"""

#: The GitHub repository, as `owner/name`.
GITHUB_REPO = "nirmallab/plexora"

#: The repository's web page.
REPO_URL = f"https://github.com/{GITHUB_REPO}"

#: Where bugs are reported.
ISSUES_URL = f"{REPO_URL}/issues"

#: The package's page on PyPI (not the JSON index the updater reads).
PYPI_URL = "https://pypi.org/project/plexora/"

#: The documentation site. Always ends in a slash, so a page is
#: `DOCS_URL + "docs/<section>/<page>/"`.
DOCS_URL = "https://nirmallab.github.io/plexora/"
