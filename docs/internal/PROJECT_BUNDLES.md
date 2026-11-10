# Project bundles: the `.plexora` file, format 2

A `.plexora` file carries one project or many (a whole dataset, with its
figures and bookmarks) from one Plexora to another: website to desktop, one
computer to the next, one person to another, often across a shared drive.
The code is `plexora/server/models/bundles.py`. Its routes are in
`server/routes/bundle_routes.py`, and the command line is
`plexora project export|import`.

Format 1 (`plexora-project.json` plus `project/<path>`) is read-only now. It
still imports, through the same pipeline, with its database rows renamed.

## The file

The file is a ZIP64 archive that any unzip tool opens. It uses deflate, not
zstd, because Python's `zipfile`, Finder and Explorer, and the browser's
`DecompressionStream("deflate-raw")` all read deflate.

```
plexora-bundle.json                 manifest: the first member, stored
projects/<id>/entry.json            registry entry, paths tokenised
projects/<id>/state/<rel>           the project's own folder; <name>.db as an sqlite backup snapshot
projects/<id>/derived/<rel>         opt-in "ready to open": files Plexora made that the entry names
media/<key>/<name>                  opt-in: the user's images, masks and tables
captures/<id>.json                  bookmarks and captures of project <id> (rows, no pictures)
captures/<id>/<n>.<fmt>             their pictures
figures/<figure id>/...             a figure whose images all come from projects in the file
```

`<id>` is the project's id: `id` in its registry entry, `uuid4().hex`.

`<key>` is the first 16 hex characters of the sha256 of the image's
identity: its name, size and the hashes of its two ends (`sources.file_identity`),
or a folder's listing hash. It is not the sha256 of the image's bytes, which
is only known after the image has streamed, and the path has to be decided
before the image is written. The real sha256 is in the manifest.

### Compression

Each member is either stored or deflated:

- **Stored:** `.png .jpg .jpeg .webp .parquet .gz .zst .zip .mp4 .h5ad .npz
  .jp2`. These are compressed already.
- **Otherwise:** the first 4 MiB is deflated at level 1 as a sample. If the
  sample keeps more than 90% of its size, the member is stored. Otherwise it
  is deflated at level 6.

So uncompressed multiplex OME-TIFFs and CSVs are compressed, and JPEG-tiled
slides are not. A member of 1 GiB or more is written with ZIP64 sizes from
the start.

### The manifest

```
{format: 2,
 bundle:   {id, created, app: {kind: desktop|website|server, version, at}},
 projects: [{id, name, title, revision, parent,
             entry: {path, size, sha256},
             files: [{path, size, sha256, kind: state|table|derived}],
             refs:  [{token, role, roles?, kind: file|folder|node, identity,
                      included: <media path>|null, derived,
                      hints: {absolute, relative_to_bundle, at, recorded}}]}],
 media:    [{path, size, sha256}],
 captures: [{project, path, size, sha256, count, previews: [{path, size, sha256}]}],
 datasets: [{id, name, projects: [project id], description, meta}],
 figures:  [{id, title, projects: [project id], files: [{path, size, sha256}]}],
 left_out: [{project, path, why}]}
```

The manifest's place is kept free first and filled in at the end. The writer
streams every other member, takes each one's sha256 and CRC as it goes,
writes the finished manifest into the room it kept (padded with spaces,
which is still valid JSON), and corrects the local header's CRC. It then
closes the file and reads the central directory back to check every size
and CRC. Only after that check is the `.part` file renamed into place.

### Tokens

The entry is written with tokens wherever a path belongs to one computer:

| Token | Stands for |
|---|---|
| `{{state}}/<rel>` | a file in the project's own folder (`paths.project_state_dir`) |
| `{{derived}}/<rel>` | a file Plexora made, carried (`paths.derived_root`) |
| `{{ref:N}}` | a file outside the project: `refs[N]` |
| `/generated/data/{{name}}/` | the project's tile addresses |

On import each token is replaced with the target's paths. A `{{ref:N}}`
becomes the path the file was recorded under (`hints.recorded`). Its
identity goes into the entry's `sources` map, with `at[<this computer>]` set
to wherever the file was found. That is how `sources.localize` already shows
each app its own path. A ref that was not found keeps its identity, so
`relink.ensure` (at open) and the website's "Find them…" go on looking for it.

`sources`, `origin`, `bundleRevision` and `lastOpenedAt` describe the
exporting computer's own copy, not the project. `sources` is rebuilt from the
refs on import. The other three are left out of the revision.

### What goes, what stays

Every file in the project's folder is carried, except for these:

- **The caches**, which are rebuilt when the project opens. `bundles.CACHES`
  is one tuple, used by both the export and the size estimate: the spatial
  index and `ball_tree.pickle`, `centroids_v*`, `transcripts_v*`, `bins_v*`,
  `volume_v*`, `objects_planes_v*`, `*_pyramid.zarr`, the classifier's
  feature cache, QC score fields (`plugins/qc/**/*.npz`), the thumbnail,
  open marks, `*.bak*`, `*.corrupt-*`, locks, temporary and journal files.
- **The computer's own records.** These are never carried: `nodes.json`,
  `remotes.json`, `servers.json`, tokens, `.telemetry`, licence files,
  `.remote_cache`, `.agent/` and `.videos/`.

Files the entry names that are inside the project are sorted by what they
are:

- **Tables Plexora made** go by default. Examples are the edited copy of a
  cell table and a corrected parquet. These exist nowhere else.
- **Derived files** go only with "ready to open". Examples are a converted
  10x `tenx/*.h5ad`, a `*_tiled.ome.tif`, a pyramid and a pinned projection.
  When they are left out, they are carried as refs, so a computer that can
  see the original folder (a shared drive) still finds them.

Files outside the project are refs. They go into `media/` only when
"Images, masks and source tables" is on. A ref on a data node
(`resources.provider: "node"`) is carried as `{node, resource_id,
fingerprint}` and opens when that node is connected. Node addresses and
tokens are never carried.

The root-level stores go along with the projects:

- **Datasets.** The membership of the bundled projects, from
  `<root>/datasets.json`.
- **Captures and bookmarks.** Rows of `.captures/captures.db` whose
  `datasource` is a bundled project.
- **Figures.** A figure goes when every image in it (`sources[*].datasource`)
  comes from a bundled project. The figure's `exports/` folder is left out.

## Revisions and statuses

Every carried file's sha256 is taken while it streams; nothing is read twice.

A project's `revision` is the sha256 of its canonical tokenised entry plus
its sorted `(path, sha256)` pairs, counting only its `state` and `table`
files. The canonical entry is JSON with sorted keys, without `id`,
`sources`, `origin`, `bundleRevision` and `lastOpenedAt`. Leaving the images
and ready-to-open files out means the same project has the same revision
whether or not they were included.

Both export and import record where the project now stands, in the entry's
`bundleRevision: {id, parent, stamp, at}`. `stamp` is a stat-only
fingerprint: the canonical entry plus each carried file's size and modified
time. A project whose stamp still matches is known to be at that revision
without reading a byte. One that no longer matches has changed since. Import
also records `origin: {bundle, revision, importedAt, app}`. The next export
names the recorded revision as its `parent`, or keeps the old parent when
nothing changed.

The import dialog compares projects by id:

| Status | When | Default action |
|---|---|---|
| `new` | no project here has its id | import |
| `renamed` | new, but its name is taken | import as `name_2` |
| `already_here` | the local project is unchanged at the incoming revision | skip |
| `update` | the incoming `parent` is the local revision, and the local project is unchanged | update |
| `conflict` | anything else ("Changed on both") | keep both, or replace |

`update` and `replace` first move the local state to
`<root>/.replaced/<name>-<stamp>/`, with its registry entry beside it as
`entry.json`. The result names that folder. "Keep both" gives the incoming
project a new id and the next free name, which `_free_name` picks: a name in
no registry, whose folder in the target root is missing or empty.

## Import, step by step

`preview(source, into)` reads only the manifest. `apply(source, choices,
into)` then works through these steps:

1. **Validate.** Only members the manifest lists are read. Each path must be
   relative, with no `..`, drive letter, backslash or NUL, and no member may
   be a symlink (`external_attr`). A member over 1 MiB that inflates more
   than 1000 times is refused. All of them together must fit in 95% of the
   target's free space.
2. **Stage** into `<root>/.importing-<bundle id>/`, checking each member's
   sha256 as it unpacks.
3. **Rename database rows.** When the name changes, the code runs
   `UPDATE "<t>" SET datasource=? WHERE datasource=?` for every table with a
   `datasource` column, after deleting rows of any other name.
   `database_model` keys each table's one row by `datasource TEXT UNIQUE`.
   Format 1 renamed only the file, so a renamed import opened with no
   channels, gates, regions or QC.
4. **Detokenise** the entry.
5. **Place** the files into the target's folder and its derived folder.
   Included images go to the project's `media/<key>/<name>`.
6. **Merge** datasets (by id, then by name), captures (with the project name
   rewritten, and a new capture id if one is taken) and figures (with each
   image's project rewritten, and a new figure id if one is taken).
7. **Write the registry** last, under `config_transaction`.

A failure or a cancel at any step undoes every step before it. The staging
folder goes in every case.

The target root is the Location the dialog names
(`library.root_for_new_projects`). Without one, it is where new projects go
(`paths.new_projects_root`), never just `data_root()`.

### Finding the images

The importer tries each of these in turn, and takes the first candidate that
`sources.same_file` accepts:

1. **The same absolute path.** The path as recorded, as this computer
   recorded it (`at`), or as the exporter saw it.
2. **`relative_to_bundle`.** The image's path relative to the folder the
   bundle was saved in, when both share more than the top of the disk. For
   example, a bundle saved to `/Volumes/lab/exports/` records
   `../images/a.tif`, which resolves again from `//server/lab/exports/`.
3. **The tail of the path** under the shared roots, the Locations, and (on
   macOS) the mounted volumes. Longest tails are tried first.
4. **`relink.resolve`.** The system file index, then a walk of the user's
   folders and the bundle's folder, for `FIND_SECONDS`.

A folder source (an OME-Zarr or a SpatialData store) has a folder identity:
`kind: "folder"`, its file count, total bytes, newest modified time, and a
hash of its sorted listing (`sources.folder_identity`). Its `fingerprint`
matches `segmentation_pyramid.source_fingerprint`'s folder form.

"Find missing images…" searches one folder the user picked, and nothing else
(`relink.resolve_in`, `relink.find_in_folder`).

## Routes and jobs

| Route | Purpose |
|---|---|
| `GET /projects/export/estimate?ids=` | sizes for the dialog (ids or names; `include`, `dataset`) |
| `POST /projects/export` `{ids\|names, include, dest?, dataset?}` | start an export job |
| `GET` / `DELETE /projects/export/<job>`, `POST .../cancel` | status, or stop the job |
| `GET /projects/export/<job>/file` | the file, with Range, when the export had no `dest` |
| `POST /projects/export/plan` | the website's member list |
| `POST /projects/export/manifest` `{manifest, sums}` | the finished manifest |
| `POST /projects/import/preview` | preview a `{path}`, `{upload}`, `{manifest}`, or the file itself |
| `POST /projects/import` `{path\|upload, choices, location}` | start an import job |
| `GET` / `DELETE /projects/import/<job>`, `POST .../cancel` | status, or stop the job |
| `POST /projects/<name>/copy` `{location?}` | "Make a copy" (a job; poll `/projects/import/<job>`) |
| `POST /projects/<name>/find_missing` `{folder}` | search one folder for missing images |

The jobs run as `layer_jobs` tasks owned by `project-bundle`. Export goes
through Snapshot, Pack, Verify. Import goes through Verify, Unpack, Place.
Each task is mirrored to the website's page, whose cancels are POSTs, so each
cancel has a POST form too. The older routes (`GET /project/<name>/bundle`
and `POST /projects/bundle`) are thin wrappers over the same core. They
stream through files, never memory. `POST /desktop/open` on a `.plexora`
file now answers `{open: "bundle", path, url: "/open_project?bundle=…"}`
instead of importing without asking.

## The website

The website's Python has a 4 GiB heap, and its large files are not in it. So
there, Python decides and the page moves the bytes.
`plan_for_page(ids, include)` writes the database snapshots, entries and
captures into a work folder. It returns every member as `{bundle_path,
source, kind, size, project}`, where `source` is a path in Python's
filesystem: `/data/...`, `/picked/<id>/...`, or the work folder. With the
members come the tokenised entries and the manifest skeleton. The page's
worker streams each member, computing CRC32 and SHA-256 as it goes, then asks
`finish_manifest(manifest, sums)` (`POST /projects/export/manifest`) for the
manifest it writes first. For import, the worker reads the central directory
itself and sends the manifest to `preview_manifest`. The plan's Phase 7 has
the rest.

## Sync later

No cloud code exists yet, but the format is ready for it:

- **Content-addressed blobs.** Every carried file has a sha256 in the
  manifest.
- **Lineage.** Every project has a revision and the revision it came from.
- **One import pipeline.** Projects already arrive through a `Source`.

A later sync uses these as follows:

1. **Push.** For each project to sync, compute the manifest as an export
   would (`plan`, then stream to compute sums; nothing needs writing locally
   but the sums). Send the server the project's manifest entry and its
   `parent`. The server answers with the sha256s it lacks, and the client
   uploads those blobs alone, keyed by sha256. A project that changed one
   gate uploads one small database snapshot. Images are uploaded only when
   the user asked for them, exactly as with a file.
2. **Pull.** Ask the server for the manifests of the projects it holds.
   `preview_manifest` gives each one's status against this computer:
   `already_here`, `update` when the server's parent is our revision, or
   `conflict`. `apply` then imports through a third Source,
   `CloudSource(manifest, fetch)`, next to `ZipSource` and `FolderSource`. Its
   `members()` is the manifest's paths, `info()` comes from the manifest, and
   `open(path)` streams the blob by sha256 from the server, or from a local
   cache of blobs this computer already has. Everything after that is the
   pipeline above, unchanged: validation, staging, row renames, tokens,
   placing, merges, and the registry written last.
3. **Conflicts** stay "keep both" or "replace". Merging two divergent
   versions of one project is out of scope, as it is for files.

Nothing in the format changes for sync. The server keeps manifests per
project id and blobs per sha256, and a bundle file is the same data with the
blobs inline.

## Measured

Still to record, by hand, in a scratch `PLEXORA_DATA_PATH`:

- the size of lsp11385_2 exported without images (its folder is about
  460 MB: a 206 MB CSV that deflates, a database of about 5 MB, and caches
  and a `.bak` file that stay out);
- the "ready to open" estimate against the real file for the visium HD run.

The test suite already checks that a 3 GB member streams into ZIP64 with a
`tracemalloc` peak under 64 MB (`tests/test_bundles.py`).
