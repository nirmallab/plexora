# Plexora

![](./plexora/client/src/img/logo_with_text.svg)

## About
This is  an [openseadragon](https://openseadragon.github.io/) based **Cellular Image Viewing and Analysis Tool**. 
It is built with a python [Flask](http://flask.pocoo.org/) backend and a [Node.js](https://nodejs.org/en/) javascript frontend.

Images are read where they are, never converted or copied on import: OME-TIFF,
TIFF, SVS and QPTIFF through the multichannel viewer, PNG and JPEG as a flat
view-only image, and **OME-Zarr / NGFF** — either a standalone `.ome.zarr` store
or an image inside a [SpatialData](https://spatialdata.scverse.org/) `.zarr`,
which can be the same store the cell table comes from. Point the Image field at
the store and Plexora finds the image group inside it; a store that arrives with
too few resolution levels to zoom out of gets the missing coarse ones derived
once, into the project's own directory.

## Install (for Users)

```bash
pip install plexora
plexora
```

That is the whole setup. `plexora` starts the server, prints the URL, and opens
a browser when the environment looks interactive. On the first run it also
prints where it will keep your projects.

```bash
plexora my_dataset        # open a project straight away
plexora --port 9000       # a specific port (8000 is the default; if it is
                          # busy, Plexora moves to a free one and says so)
plexora --version
python -m plexora         # same thing, when the console script is not on PATH
```

There are four ways to run Plexora, all of which end in the same viewer:

| Where you are | What to run |
| --- | --- |
| Your own machine, a terminal | `plexora` |
| Your own machine, a notebook | `plexora.view("my_dataset")` |
| A remote machine you can ssh into | **Settings → Remote servers → Connect** — or `plexora connect user@host` |
| A hosted notebook or an HPC terminal | `plexora` — it works out where it is running and prints a URL that works |

Save a remote server once and reconnecting is a button: Plexora handles the SSH
connection, the ports, the tunnel and the URL, and relays a password or 2FA
prompt to the page if the server asks for one. It never stores the password.

### The desktop app

Plexora also comes as an ordinary desktop application for Windows, macOS and
Linux -- no Python, no terminal. Download the installer for your system from
the [releases page](https://github.com/nirmallab/plexora/releases):

| System | File |
| --- | --- |
| Windows 10/11 | `Plexora-<version>-windows-x64-setup.exe` (installs for you only; no admin rights) |
| macOS 12+ on Apple silicon | `Plexora-<version>-macos-arm64.dmg` |
| macOS on Intel | not available: llvmlite, which spatialdata needs through numba, no longer publishes Intel Mac builds |
| Linux (Debian, Ubuntu) | `Plexora-<version>-linux-x64.deb` |

It is the same Plexora -- the same viewer, plugins and remote connections --
in its own window, with native menus and file dialogs. Drag a slide, a run
folder or a table onto the window to import it; it is read where it lies, never
copied or uploaded. `.svs`, `.ndpi`, `.scn`, `.mrxs`, `.qptiff` and `.h5ad`
files get an "Open with Plexora" entry. **File → Open in Browser** opens the same
session in your web browser. The app keeps its projects in the same place
`plexora` in a terminal does, so the two share one library.

The installers are not code-signed yet. On Windows, SmartScreen asks once
("More info" → "Run anyway"). On macOS, right-click the app and choose
**Open** the first time, or run `xattr -dr com.apple.quarantine /Applications/Plexora.app`.

**[docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)** is the step-by-step version for
people who would rather not learn about tunnels — organised by where your data
is, with a compatibility matrix and a screenshot for every screen.
**[DEPLOYMENT.md](DEPLOYMENT.md)** is the same ground for a technical reader,
from a fresh conda environment to HPC job submission, with the real output of
each command. The rest of this README is the short version.

### Where your data lives

Plexora keeps projects, figures and settings in one directory, chosen in this
order:

| Rule | Location |
| --- | --- |
| `--data-dir` / `PLEXORA_DATA_PATH` | whatever you pass, for that one command |
| a recorded setting | `plexora config set data-dir <path>` |
| a directory suggested by a connection | the saved profile that launched the session, used only if nothing above answered |
| default | `%LOCALAPPDATA%\plexora`, `~/Library/Application Support/plexora`, or `~/.local/share/plexora` |

It never depends on the directory you started from, and never lives inside the
installed package.

```bash
plexora where                          # which directory, and which rule chose it
plexora config set data-dir /scratch/me/plexora
```

Moving it matters on HPC and on Windows machines with a small system drive:
derived image pyramids are large, and the default sits on your home or system
volume. `plexora config set data-dir` records the choice permanently; the
environment variable overrides it for one run.

### Datasets: grouping projects that belong together

One image is one project. A **dataset** is the folder above that -- a patient
cohort, a TMA series, one imaging run -- and it is nothing more than a grouping:
it holds project names, not data. Deleting a dataset releases the projects in
it and touches no file of theirs, and taking a project out of one is not
deleting the project.

On the Open Project page, datasets appear as folders. Open one to see what is
in it, drag cards onto one to move them, and drag them onto *All projects* to
take them out again. Select several with the tick in a card's corner, or with
ctrl-click and shift-click, and move them together. Searching looks everywhere
and tells you which dataset each result lives in.

The same thing from Python or the terminal:

```python
import plexora

plexora.create_dataset("Melanoma Cohort", images=[
    "sample1.ome.tif", "sample2.ome.tif", "sample3.ome.tif",
])
```

```bash
plexora dataset create melanoma_cohort --images sample1.tif sample2.tif
plexora dataset list
plexora dataset show melanoma_cohort
```

### Register first, configure as you go

**An image is the only thing a project must have.** Everything else -- a
segmentation mask, a feature table, which table inside a `.zarr` store, which
column holds the cell id -- is optional at registration, and Plexora asks for
what it needs at the moment something needs it rather than up front:

```python
plexora.create_project("slide.ome.tif")                       # complete already
plexora.create_project("slide.ome.tif", data="store.zarr")    # table undecided
```

The second is a valid project: it opens, it shows the image, and the first
tool that wants a table asks which one to load, with the path already filled
in. Answer once and nothing asks again.

Anything you *do* know can be said at registration, and is then recorded as an
answer rather than a guess -- so nothing asks you to confirm it later:

```python
plexora.create_dataset("Melanoma Cohort", projects=[
    {"image": "sample1.ome.tif", "segmentation": "sample1_mask.tif",
     "data": "sample1.csv", "cell_id": "CellID",
     "x": "X_centroid", "y": "Y_centroid"},
    {"image": "sample2.ome.tif"},
])

plexora.configure_project("sample2", data="sample2.csv", cell_id="CellID")
plexora.project_manifest("sample2")   # what it has, and what is still open
```

Files do not all have to be on the same machine. `node` names a data node you
are connected to, and any field can say otherwise for itself -- so a cohort
whose slides sit on a cluster and whose quantification sits on your laptop is
one document:

```python
plexora.create_dataset("Melanoma Cohort", node="hms-o2", projects=[
    {"image": "/n/scratch/reg/sample1.ome.tif",
     "segmentation": "/n/scratch/seg/sample1.tif",
     "data": {"path": "C:/quant/sample1.csv", "node": None}},
    {"image": "/n/scratch/reg/sample2.ome.tif"},
])
```

The projects are registered here, so they appear in the Samples page like any
other; the bytes stay where they are and are read through the node.

The same options exist as flags:

```bash
plexora project create slide.ome.tif --data cells.csv --cell-id CellID \
    --markers CD3 CD8 --dataset melanoma_cohort
plexora project show slide            # every question, and whether it is answered

plexora dataset create "Melanoma Cohort" --from projects.json --node hms-o2
```

### Shared projects

Several people on one workstation or login node can share a directory of common
datasets while keeping their own work private:

```bash
export PLEXORA_SHARED_PATH=/srv/plexora/common   # or: plexora config set shared-dirs ...
plexora
```

Shared projects appear in Open Project marked *Shared*. They can be opened and
explored but not edited or deleted, and everything you produce while exploring
one — gates, ROIs, figures, cached results — is written to **your** data
directory, not the shared one. A project of your own with the same name takes
precedence over the shared copy.

Plexora has no user accounts. For a multi-user deployment, run one process per
user behind a reverse proxy that maps the authenticated user to their own
`--data-dir`, and keep the server bound to loopback (the default). The one
place Plexora authenticates at all is where it cannot use loopback — the Open
OnDemand routes, which mint a per-server token — and that protects a
single-user server rather than telling several users apart.

## Running on a remote machine over SSH

Plexora binds to loopback and has no authentication, so the way to reach one
running on a server is an SSH tunnel rather than an open port. There are two
ways to get one, and they do the same thing.

**Have Plexora set it up.** Run this on *your own* computer:

```bash
plexora connect user@server.lab.edu                 # starts, tunnels, opens a browser
plexora connect user@server.lab.edu my_dataset      # …straight into a project
```

It stays in the foreground; Ctrl+C closes the tunnel and stops the remote
server. It uses your system `ssh`, so whatever `~/.ssh/config`, an agent, a
ProxyJump or a hardware token already do for `ssh user@host` happens here too.

If the remote `plexora` is not on a non-interactive `PATH` — which is common
with conda — name it explicitly:

```bash
plexora connect user@server.lab.edu --remote-command /home/you/miniconda3/envs/imaging
```

The environment path is enough — `bin/plexora` is filled in for you.

**Or do it by hand.** On the remote machine:

```bash
plexora --remote
```

It prints the exact `ssh -N -L …` command to paste into a terminal on your own
computer, and the `http://localhost:<port>/` address to open afterwards.

### HPC clusters with compute nodes

On a cluster you usually may not run anything heavy on the login node, so
Plexora belongs in a job. `--srun` submits one and tunnels to whichever node
the scheduler grants — the target is the *login* node:

```bash
plexora connect user@o2.hms.harvard.edu --srun "-p interactive -t 4:00:00 -c 16 --mem 128G"
```

Allocation may queue; it says so while it waits. Ctrl+C ends the job.

By hand, the same thing is two steps: start an interactive job
(`srun --pty -p interactive -t 4:00:00 -c 16 --mem 128G bash`), then run
`plexora --remote` inside it. It detects the job, works out which compute node
it is on and which login node you came through, and prints the two-hop
`ssh -J` command for it.

Some sites refuse SSH into a compute node. Add `--bind-node` at either end for
a login-node forward instead — note that this makes the port reachable from
the cluster's internal network while it runs.

A lab's shared reference data pairs naturally with this: point
`PLEXORA_SHARED_PATH` (or `plexora config set shared-dirs`) at a read-only
directory on the cluster filesystem, and `--data-dir` or
`plexora config set data-dir` at your own scratch space.

## Running as a Docker container
**Note:** When running on an ARM machine (e.g. M1 Macbook), build the image with `docker build --platform linux/amd64 -t plexora .`
* Build image: `docker build -t plexora .` 
* Run image with mounted path: `docker run --rm -dp 8000:8000 -v [source path]:/[target path] plexora`

where
* `--rm` cleans up the container after it finishes executing
* `-v` mounts the "present working directory" (containing your data) to be `/data` inside the container. This is necessary in order to import your data via the import page.
* `-dp` forwards the port 8000

Once the container is running, go to `http://localhost:8000/` in your web browser. 
To import your imaging files in the import gui type in the mounted `/data/..`

Inside the image, projects are written to `/app/data` (`PLEXORA_DATA_PATH`), so
mount a volume there to keep them between runs:

```bash
docker run --rm -dp 8000:8000 -v ~/plexora-data:/app/data -v /my/images:/data plexora
```

The image also sets `PLEXORA_HOST=0.0.0.0` — published ports would never reach
it otherwise — and `PLEXORA_DOCKER=1`, which switches the import page to
container-shaped path hints.

## Data on more than one machine

Sometimes the image and the cell table are not on the same computer — the slide
is on cluster scratch and the `.h5ad` came back to your laptop. Start a **data
node** where the data is:

```bash
plexora node serve --serve image:tumor=/scratch/me/tumor.ome.tif
```

It prints a token. Register the node in the viewer under **Settings → Data
nodes**, then point a project at it from that project's **Edit** page, under
*Where the data lives*.

A node is a Plexora with the viewer switched off: it holds files and answers
questions about them. Your projects, ROIs, gates and figures all stay on the
machine you are looking at, so a node can restart or disappear without any of
your work being at risk — the project still opens, and whatever came from that
node is absent and says so.

See [DEPLOYMENT.md](DEPLOYMENT.md#7-data-on-more-than-one-machine) for the
tunnel recipes, what actually crosses the network, and the limits.

## Clone and Run Codebase (for Developers)

```bash
git clone https://github.com/nirmallab/plexora.git
cd plexora
python -m venv .venv && source .venv/bin/activate   # or conda create -n plexora python=3.13
pip install -e ".[dev,jupyter]"
```

Any Python 3.12 or 3.13 environment works — conda, venv, uv, whatever you
already use. The editable install pulls every runtime dependency plus the test
and notebook extras; there is no separate environment file to keep in sync.

Then start the server with `python run.py` (or `plexora`) and open
`http://localhost:8000/`.

## Running in Jupyter notebooks

Install the package into the same environment as Jupyter:

```bash
pip install "plexora[jupyter]"
```

Then, in a cell:

```python
import plexora

plexora.view("my_dataset")
```

That is the whole thing, in every kind of notebook. Plexora starts a small
server beside your kernel and shows it in the cell.

`data_dir` is optional everywhere below: leaving it out uses the same directory
`plexora where` reports, so a notebook and a terminal see the same projects.
Pass it to work against a different one.

### Hosted notebooks — JupyterHub, Open OnDemand, Colab

The same call. When your kernel is not on the machine with your browser, a
`127.0.0.1` address would point at your own laptop, so Plexora detects the
situation and builds the proxied URL your host actually serves it on.

On a JupyterHub server, this needs `jupyter-server-proxy` installed **in the
environment running the Jupyter server** (not necessarily the one running your
kernel):

```bash
pip install jupyter-server-proxy
```

**Open OnDemand needs nothing installed.** Plexora recognises the portal and
mounts itself under `/rnode/<node>/<port>`, the door OnDemand provides for apps
that serve at the root. That door is reached from the portal over the network,
so the viewer binds `0.0.0.0` and protects itself with a token carried in the
URL — it says so, once, when it starts. From a terminal in the same session,
`plexora --ood my_dataset` does the same thing for the standalone app.

Colab needs nothing extra either. Neither does local Jupyter or VS Code Remote,
which keep the direct localhost address they always used.

Override the detection if you need to:

```python
plexora.view("my_dataset", proxy=True)     # always proxy
plexora.view("my_dataset", proxy=False)    # always use 127.0.0.1
plexora.view("my_dataset", base_url="/user/me/")   # name the prefix yourself
```

On a hub, `viewer.url` is a path rather than a full address — open it under
your notebook server's own address, which is where your session is
authenticated.

Datasets can also be registered directly from notebook-visible files:

```python
from plexora.jupyter import PlexoraViewer

viewer = PlexoraViewer.from_files(
    name="my_dataset",
    image="/path/to/image.ome.tif",
    segmentation="/path/to/segmentation.ome.tif",
    features="/path/to/cells.csv",
    x="X_centroid",
    y="Y_centroid",
    id_column="CellID",
)
viewer
```

### Looking at what is already in your kernel

`plexora.view` takes data as well as a name, and each piece may be a path **or
an object you already have**:

```python
import plexora, scanpy as sc

sc.tl.leiden(adata)

plexora.view(
    "my_dataset",
    image="/path/to/slide.ome.tif",   # a path, read from disk as always
    segmentation=mask,                # a numpy label array
    adata=adata,                      # served straight out of this kernel
    tool="cell_explorer",             # open with this panel showing
    overlay="leiden",                 # ...drawn by this column
    channels=["DAPI", "CD3"],         # ...over these channels
)
```

Nothing is written to disk. The kernel serves its own objects to the viewer
over Plexora's data-node API, on a loopback port that only the viewer beside it
can reach — so this works the same in local Jupyter, JupyterHub, Open OnDemand
and Colab, with no extra port exposed anywhere.

Annotate again and show the result without rebuilding anything:

```python
adata.obs["phenotype"] = classify(adata)
viewer.refresh(adata)
```

A refresh re-reads the table's shape, so a column that did not exist a cell ago
is immediately available as an overlay. Anything you do not name is left alone
— a whole-slide image is not re-read and the tiles your browser is holding stay
valid.

`tool`, `overlay` and `channels` belong to that one viewer. They are carried in
its URL and never overwrite the channels, colours or overlay the project has
saved, so a notebook can open the same project a dozen ways without disturbing
what you last set up by hand.

A few things worth knowing:

- **`table=` takes a pandas DataFrame** wherever `adata=` takes an AnnData.
  `sdata=` takes a SpatialData object and pulls its image, labels and table
  apart for you (name them with `sdata_image=`/`sdata_labels=`/`sdata_table=`
  when the store holds more than one of a kind).
- **Very large images belong on disk.** A path costs nothing here; an array is
  given the coarse pyramid levels a viewer needs, which is about a third of it
  again in memory. Plexora says so if the array is large.
- **A table snapshot is a copy** of the elements the viewer reads — `obs`,
  `var`, and the one matrix you chose. For an imaging table that is tens of
  megabytes. `to_disk=True` restores the old behaviour of writing an `.h5ad`,
  which is the better trade when the project should outlive the kernel.
- **The other matrices are not copied**, so a memory-served project does not
  offer them as a read spec to switch to. Reading a different layer means
  calling `plexora.view` again saying so.

## Baseline smoke test

Before upgrading dependencies or changing the viewer/server boundary, run the local `orion2` baseline:

```bash
python -m tests.baseline_orion2
```

The test checks Flask app import, `/config`, the viewer page, metadata JSON, channel metadata, and one image tile plus one segmentation tile. It skips with a clear message if the local `orion2` datasource or exemplar files are not available.


#### (4. Node.js installation and packages)
  This step is only needed when you plan to edit js code. The codebase already included bundled js files.
* Install [Node.js](https://nodejs.org/en/), then navigate to `/plexora/client` and run `npm install` to install all packages listed in package.json.
* Run `npm run build` to package the Javascript (a production bundle, which is what is committed), or `npm run watch` while editing dependencies


## Releases and the desktop app (for Developers)

One script builds everything a release ships -- the wheel and sdist, and a
desktop installer for the machine it runs on:

```bash
python scripts/release.py doctor          # what is installed, and the line that installs what is not
python scripts/release.py all --dev       # wheel, runtime, shell, installer, then launch it to check
python scripts/release.py ci --bump patch # tag v<X.Y.Z> and let GitHub build all three systems
```

The version lives in `pyproject.toml` only; `release.py bump` (or `propagate`)
copies it into the desktop app's config. Pushing a `v<X.Y.Z>` tag runs
`.github/workflows/release.yml`, which builds the Windows, macOS (arm64 and
x64) and Linux installers and publishes them with the wheel. The app is a
[Tauri](https://tauri.app) shell (`desktop/`) around an embedded Python that
runs `plexora --desktop`; DEPLOYMENT.md has the details, signing included.

## License

Plexora is released under the **Plexora Academic License 1.0** (see [LICENSE](./LICENSE)).
It is **not** an open source license. In short:

| | |
|---|---|
| Academic research, teaching, personal study | ✅ Free |
| Use by a nonprofit or government research institution | ✅ Free (whatever the funding source) |
| Redistributing Plexora unmodified, with the license attached | ✅ Allowed |
| Patching your own copy to fix a bug or a compatibility problem | ✅ Allowed |
| Publishing a fork, a patched build, or a renamed version | ❌ Not allowed |
| Commercial use of any kind | ❌ Requires a paid license |

**Plugins are a deliberate exception.** Anything you build against the documented
extension interfaces — the `plexora.plugins` entry point group and the `plexora.api`
package — is yours. You may distribute and sell your plugin under whatever license
you like, and you do not need our permission. Extending Plexora through the plugin
API is the supported way to change what it does; editing its source is not.

For a commercial license, contact Ajit Johnson Nirmal <ajitjohnson.n@gmail.com>.

Some bundled components carry their own licenses, which are unaffected by the above —
see section 8 of [LICENSE](./LICENSE).
