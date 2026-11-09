"""Where Plexora points people: the repository, the issue tracker, PyPI, the
documentation site, and the BioCognia account portal.

One module so the browser (Help menu, a plugin's help button), the updater and
the desktop shell cannot drift apart. `base.html` renders `DOCS_URL` into the
page for the client, and tests/test_docs_links.py holds the desktop menu's
copy in `desktop/src-tauri/src/menu.rs` to this value.

The documentation site is GitHub Pages with the custom domain plexoraapp.com
(since 2026-11-09), so the old https://nirmallab.github.io/plexora/ addresses
in earlier builds redirect to the same page here.
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
DOCS_URL = "https://plexoraapp.com/"

#: Where a person signs in, buys, starts a trial and approves a device: the
#: BioCognia portal every BioCognia product shares. Plexora holds no account
#: of its own; the licence client (`biocognia`) uses the same address,
#: `BIOCOGNIA_PORTAL` overriding it.
ACCOUNT_URL = "https://account.biocognia.com"

#: Buy, activate or start a trial of Plexora (the onboarding contract's link).
START_URL = f"{ACCOUNT_URL}/start?product=plexora"

#: Where Plexora Pro's prices are, and Buy, which opens checkout in the
#: portal: the pricing section of biocognia.com. The documentation site
#: covers the free app only. A seat bought there comes with a licence key by
#: email, kept under Licences in the portal.
PRICING_URL = "https://biocognia.com/#pricing"

#: The website: Plexora in a browser tab, nothing installed. A project's page
#: there is this plus the project's name (plexora/website_link.py).
WEBSITE_URL = "https://viewer.plexoraapp.com/"

#: The website's switches (`tools/browser_build/publish.py flags`): `viewer`
#: "on" once it is open to everyone (the GA cut-over), "preview" before,
#: "desktop-only" when it is paused. The desktop app shows "Open on the
#: Website" only while it says "on".
WEBSITE_FLAGS_URL = "https://assets.plexoraapp.com/flags.json"
