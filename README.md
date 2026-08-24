# Plexora

![](./plexora/client/src/img/logo_with_text.svg)

## About
This is  an [openseadragon](https://openseadragon.github.io/) based **Cellular Image Viewing and Analysis Tool**. 
It is built with a python [Flask](http://flask.pocoo.org/) backend and a [Node.js](https://nodejs.org/en/) javascript frontend.

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
| Your own machine, a terminal | `plexora`, or a [release executable](#executables-for-users) |
| Your own machine, a notebook | `plexora.view("my_dataset")` |
| A remote machine you can ssh into | `plexora connect user@host` — or `plexora --remote` on the host |
| A hosted notebook (JupyterHub, Open OnDemand, Colab) | `plexora.view("my_dataset")` — the proxy is detected for you |

**[DEPLOYMENT.md](DEPLOYMENT.md) walks through all of them in detail**, from a
fresh conda environment to HPC job submission, with the real output of each
command. The rest of this README is the short version.

### Where your data lives

Plexora keeps projects, figures and settings in one directory, chosen in this
order:

| Rule | Location |
| --- | --- |
| `--data-dir` / `PLEXORA_DATA_PATH` | whatever you pass |
| a recorded setting | `plexora config set data-dir <path>` |
| a frozen executable | beside the executable |
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

Plexora has no user accounts and no authentication. For a multi-user
deployment, run one process per user behind a reverse proxy that maps the
authenticated user to their own `--data-dir`, and keep the server bound to
loopback (the default).

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
plexora connect user@server.lab.edu --remote-command "conda run -n imaging plexora"
```

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
plexora connect user@o2.hms.harvard.edu --srun "-p interactive -t 4:00:00 --mem 16G"
```

Allocation may queue; it says so while it waits. Ctrl+C ends the job.

By hand, the same thing is two steps: start an interactive job
(`srun --pty -p interactive -t 4:00:00 --mem 16G bash`), then run
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

## Executables (for Users)
Releases can be found here:
https://github.com/nirmallab/plexora/releases
These are executables for Windows and MacOS that can be run locally without any installations.


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

## Clone and Run Codebase (for Developers)
#### 1. Checkout Project
* `git clone https://github.com/nirmallab/plexora.git`
* `cd plexora`
#### 2. Checkout Necessary Branch
* `main` is the core application (no gating module).
* **For Gating, run** `git checkout addon/gating`
* Run `git pull` to make sure everything is up to date 



#### 3. Conda Install Instructions. 
##### Install Conda
* Install [miniconda](https://conda.io/miniconda.html) or [conda](https://docs.conda.io/projects/conda/en/latest/user-guide/install/download.html). 
* Create env:  `conda env create -f requirements.yml`

##### Activate Environment
* Active environment: `conda activate plexora`


##### Start the Server

* `python run.py` - Runs the webserver
##### Start the Server

* Access the tool via `http://localhost:8000/`

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

On a JupyterHub or Open OnDemand server, this needs `jupyter-server-proxy`
installed **in the environment running the Jupyter server** (not necessarily
the one running your kernel):

```bash
pip install jupyter-server-proxy
```

Colab needs nothing extra. Neither does local Jupyter or VS Code Remote, which
keep the direct localhost address they always used.

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

## Baseline smoke test

Before upgrading dependencies or changing the viewer/server boundary, run the local `orion2` baseline:

```bash
python -m tests.baseline_orion2
```

The test checks Flask app import, `/config`, the viewer page, metadata JSON, channel metadata, and one image tile plus one segmentation tile. It skips with a clear message if the local `orion2` datasource or exemplar files are not available.


#### (4. Node.js installation and packages)
  This step is only needed when you plan to edit js code. The codebase already included bundled js files.
* Install [Node.js](https://nodejs.org/en/), then navigate to `/plexora/client` and run `npm install` to install all packages listed in package.json.
* Run `npm run start` to package the Javascript, or run `npm run watch` if you plan on editing dependencies


## Packaging/Bundling Code as Executable (for Developers)
Any tagged commit to a branch will trigger a build, where `tag == commit message`. This will appear under releases. Note building may take ~10 min.

Tagging Conventions: All release tags should look like `v{version_number}_{branch_name}`.
