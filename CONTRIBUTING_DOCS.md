# Contributing to the documentation

The documentation site (https://nirmallab.com/plexora/, also reached at
https://nirmallab.github.io/plexora/) is built from this repository. It covers
the **free app only**: nothing about Pro, pricing, licences or trials, and no
page for a paid feature. Like the app, it is dark only (no light theme, no
switch). It has two tabs. **Docs** is the guide, written by
hand, one short page per task. **Reference** is **generated from the code**.
Knowing which is which is most of what you need.

| You want to change | Edit | Then |
|---|---|---|
| What a Python function or class page says | its docstring | `python tools/docs/sync_docs.py generate` |
| Where a symbol's page goes, its related links, parameter groups, "how is this different" boxes | `tools/docs/manifest.yaml` | `generate` |
| A command-line option's help | the `argparse` call in `plexora/cli.py` | `generate` |
| A command-line example or its Python equivalent | `tools/docs/cli_examples.yaml` | `generate` |
| An environment variable's description | `tools/docs/env_vars.yaml` | `generate` |
| The supported-formats table | the importer's suffix lists, or `format_notes` in `manifest.yaml` | `generate` |
| The browser plugin API | the JSDoc `@typedef` blocks in `plexora/client/src/js/pluginRegistry.js` and `main.js` | `npm run generate:browser-api --prefix website` |
| A guide page | `website/content/docs/(guide)/**.mdx` | nothing |
| A screenshot | its entry in `tools/docs/shots.mjs` | capture it (see Screenshots) |
| A moved or deleted page | `website/redirects.json` | nothing; the build writes the redirect |
| The download buttons | nothing: a release does it | `npm run resolve:release --prefix website` to refresh locally |

Never edit a file under `website/content/docs/reference/python-api/`,
`reference/plugin-api/descriptor/`, `reference/plugin-api/runtime/`,
`reference/plugin-api/browser/`, `reference/cli/`,
`reference/supported-formats.mdx` or `reference/environment-variables.mdx` by
hand: the next `generate` overwrites it, and CI fails while it differs from
what `generate` produces. `reference/index.mdx`, `reference/meta.json` and
`reference/plugin-api/index.mdx` are written by hand.

## Checks

```bash
python tools/docs/sync_docs.py check        # generated pages are current
pytest tests/test_docs_*.py                 # coverage, docstrings, signatures, env vars, examples
cd website && npm ci
npm run check:mdx                            # every page compiles; links and media exist
npm run build                                # also resolves the release and writes redirects
npm run check:links && npm run check:search-size
```

`sync_docs.py` imports Plexora in a child process with a throwaway data
directory (`import plexora` builds the whole server), so it never touches your
projects. The dump takes about two seconds.

What each test enforces:

- **test_docs_coverage**: every public name (`plexora.<name>`,
  `plexora.api.__all__`, `plexora.api.plugin`) is in a `manifest.yaml`
  category, under `aliases`, or under `excluded` with a reason. Adding a
  public function without deciding where it is documented fails here.
- **test_docs_docstrings**: every documented docstring follows the convention
  below. Problems are reported as codes (see `DIAGNOSTICS` in
  `tools/docs/docstrings.py`). A real exception can be waived in
  `manifest.yaml` under `docstrings.waivers`, with a reason.
- **test_docs_signatures**: the signature on each page matches the code.
- **test_docs_freshness**: runs `sync_docs.py check`.
- **test_docs_envvars**: every `PLEXORA_*` name in the source is listed in
  `env_vars.yaml`.
- **test_docs_examples**: CLI examples parse; ```` ```python runnable ````
  blocks execute.

## Docstring convention (Google style)

```python
def create_project(image, *, name=None, data=None):
    """Register one image as a project and return its name.

    A paragraph or two of explanation, in Markdown. Use `single backticks`
    for code. Link to another public name by writing it in backticks, e.g.
    `plexora.configure_project`; the generator turns it into a link.

    When to use:
        One or two sentences on when this is the right call and when another
        one is.

    Args:
        image (str | Path): The image file. The only required argument.
        name (str, optional): What to call the project. Defaults to the
            image's filename. Continuation lines are indented.
        data (str | Path, optional): A cell table (CSV, Parquet, AnnData).

    Returns:
        str: The project name, which may differ from `name` if that was taken.

    Raises:
        FileNotFoundError: If `image` does not exist.

    Example:
        ```python
        import plexora

        name = plexora.create_project("slide.ome.tif", data="cells.csv")
        ```

    Note:
        Anything a reader must not miss.

    See Also:
        plexora.import_sample, plexora.configure_project
    """
```

Rules the tests check:

- The first line is one sentence saying what the function does, in the
  imperative for functions ("Register...") and as a noun phrase for classes.
- Every parameter in the signature appears under `Args:` with the same name;
  `*args` and `**kwargs` are written `*args` / `**kwargs`. Parameters whose
  names start with `_` are private and not documented.
- A function annotated to return something has a `Returns:` section.
- Examples live in fenced ```` ```python ```` blocks. Mark a block
  ```` ```python runnable ```` when it runs as written against the small
  fixture files in `tests/doc_fixtures.py` (`slide.ome.tif`,
  `slide_mask.tif`, `cells.csv`, `cells.h5ad`) without a server, a browser
  or the network; the example test then executes it. `import_sample(...)` in
  a runnable block must pass `wait=True`.
- Dataclass fields are documented with a `#:` comment block directly above
  the field (or in an `Attributes:` section). Public module constants get a
  `#:` comment block above them.
- Recognised sections: `Args`, `Returns`, `Yields`, `Raises`, `Example(s)`,
  `Note(s)`, `Warning(s)`, `When to use`, `See Also`, `Attributes`. Any other
  `Word:` line at the left margin is reported as an unknown section.

Docstrings of public names are user documentation. Implementation history
and design rationale belong in `#` comments in the function body, where
maintainers still see them. Do not mention agents, MCP, licensing, pricing
or telemetry in public docstrings.

## Words

The UI and the code do not always use the same word. Guides that describe
the UI use the UI's words ("Import Sample…", "Thresholding"); API and CLI
pages use the code's words (project, dataset, segmentation, node). Core
Interface overview (`getting-started/interface.mdx`) states the mapping once:

| In the UI | In the code | Meaning |
|---|---|---|
| Sample | project | One image with its mask, table and layers |
| Dataset | dataset | A named group of projects |
| Thresholding | `gating` plugin | Marker thresholds per cell |
| Mask | segmentation | The label image outlining cells |

Search aliases in `manifest.yaml` (`search`, `search_aliases`) and a page's
`aliases` frontmatter make the other word findable.

## Guides

Guide pages are MDX under `website/content/docs/(guide)/`. `(guide)` is a
folder group: it adds nothing to the URL, so `(guide)/features/viewer.mdx` is
`/docs/features/viewer`. Its `meta.json` has `"root": true`, which makes the
Docs tab; `reference/meta.json` does the same for the Reference tab. Sidebar
order is the `pages` list in each folder's `meta.json`.

Every feature and plugin page has the same shape, so a reader gets it in
thirty seconds:

```mdx
---
title: "Channels and contrast"
description: "One sentence: what this page lets you do."
aliases: ["contrast", "brightness"]     # words a reader might search instead
---

One or two sentences: what it is and when you use it.

<Media src="/media/features/channels-panel.webp" alt="What is on screen" />

## How to use it

<Steps>
<Step>
### A short imperative heading
One to three sentences, with **UI names** spelled as the app spells them.
</Step>
</Steps>

## Tips

## Related

<Cards>
<Card title="Layers" href="/docs/features/layers" description="One line." />
</Cards>
```

**UI first, the command line separately.** Everything a reader needs can be
done in Plexora itself, and the page must read that way: the steps under
`## How to use it` are things you click or type in the app, in the order you
do them. When a terminal or Python can do the same, put it after the steps in
its own `## From the command line` section, opening with a sentence such as
"Everything above can also be done from a terminal; you do not need both."
Never number a UI step and a CLI step in one sequence ("1. Click Add a
server. 2. Or run `plexora connect`"): readers take numbered steps as things
to do one after another. Two ways to do one step are tabs inside that step;
independent tasks are separate `###` sections, not steps.

Plugin pages add an **Open it** line (menu and shortcut) and a `## What it
saves` section. Budgets: a feature page 250-450 words, a plugin page 350-650.
Write from the code, not from older pages: UI strings, menu names and keys are
checked against the templates and JavaScript. Plain, second person, no
marketing words, no speed claims.

Every component a page can use is registered in
`website/components/mdx.tsx`, so pages import nothing: `<Media>`,
`<Downloads>` (the three installer cards), `<Callout>`, `<Cards>`/`<Card>`,
`<Steps>`/`<Step>`, `<Tabs>`/`<Tab>`, `<PlatformTabs>`, `<WhereTabs>`,
`<Files>`/`<Folder>`/`<File>`, `<Accordions>`/`<Accordion>`, and the API
pieces the generator uses.

Link to other pages with site paths (`/docs/features/channels`). MDX is not
Markdown: escape a literal `{`, `}` or `<` outside code as `\{`, `\}`, `\<`.

### Media

```mdx
<Media src="/media/plugins/roi.webp" alt="Three categories of regions drawn on a tonsil section"
       caption="Optional." highlights={[{ x: 40, y: 380, w: 520, h: 110, label: 1 }]} />
```

`kind` is `image` (the default; click to enlarge), `animation` (a GIF or
animated WebP) or `video` (MP4 or WebM, muted, looping, playing while half of
it is on screen, never on its own under reduced motion; give it a `poster`).
All three fill the same box, the source's aspect ratio at full column width
(`width`/`height`, default 2000x1250), so replacing a screenshot with a clip
later is a change of `kind`, `src` and `poster` and nothing moves.
`highlights` draws boxes over the media in the source's own pixels, with an
optional numbered badge; they are drawn by the page, not baked into the image,
so a recapture does not need them redrawn unless the UI moved.

### Screenshots

Every image under `website/public/media/` comes from `tools/docs/shots.mjs`:
each entry is a page of the app, the clicks and keys that set it up, and the
file it becomes. `tools/docs/screenshots.mjs` drives Chrome over the DevTools
protocol (Node 22, nothing to install) at 1280x800 CSS pixels and saves each
shot at 2000x1250 as WebP.

Capture against a Plexora that cannot show a paid control and cannot touch
your own projects:

```bash
# 1. A scratch data root holding copies of the demo projects shots.mjs names
#    (project folders plus their entries in config.json; images stay where they are).
# 2. An empty licence directory, so the app runs as the free app.
BIOCOGNIA_DIR=/tmp/plx-free BIOCOGNIA_OFFLINE=1 PLEXORA_TELEMETRY=off \
  plexora --data-dir /tmp/plx-shots --port 8791 --no-browser &
curl -s http://127.0.0.1:8791/license/status     # "state": "free"
# 3. Capture everything, or one shot.
node tools/docs/screenshots.mjs --base http://127.0.0.1:8791
node tools/docs/screenshots.mjs --base http://127.0.0.1:8791 --only plugins/roi
```

A shot marked `manual: true` (the notebook cell) is taken by hand at the same
size. Only public data appears on the site.

### Moving or deleting a page

Add the old path to `website/redirects.json` (`"pages": {"/docs/old/":
"/docs/new/"}`; `"prefixes"` mirror a whole moved tree). After `next build`,
`scripts/write_redirects.mjs` writes a small page at each old path that sends
the reader on, keeping any `#anchor`; it refuses to shadow a real page or to
point at one that was not built. In-app help links (`help.docs` in a plugin's
controller) must stay real pages: `tests/test_docs_links.py` checks them.

### Downloads

The home page and Installation show the latest desktop release's installers.
`scripts/resolve_release.mjs` (run by `npm run build`) reads the latest
GitHub release and writes `website/generated/release.json`: the version and,
for each of `-macos-arm64.dmg`, `-windows-x64-setup.exe` and
`-linux-x64.deb`, the asset's real URL and size. The file is committed, so an
offline build still works; in CI a failed lookup fails the build. The docs
workflow runs after every successful Release, so the buttons follow a release
without anyone editing the site, and it checks that every download URL and the
demo video still answer.

## Previewing

```bash
cd website
npm ci
npm run dev          # http://localhost:3000/plexora/
```

`npm run build` writes the static site to `website/out/`. To preview it at its
real sub-path, serve a directory containing `out/` renamed to `plexora/`.

## Deploying

`.github/workflows/docs.yml` builds on every pull request and deploys to
GitHub Pages on every push to `main`, on a manual run, and after every
successful Release. Moving to a custom domain is a
`website/public/CNAME` file plus the domain in the repository's Pages
settings; the base path comes from the Pages action automatically, and
`plexora/links.py` `DOCS_URL` is the one constant the app uses.
