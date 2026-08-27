# Running Plexora

Plexora is a local web application: a Python server you start yourself, and a
browser pointed at it. Everything in this guide is a variation on that one
sentence — the only thing that ever changes is **where the server runs** and
**how your browser reaches it**.

There are five places it can run. Find yours:

| Where the data and the compute are | What you run | Section |
|---|---|---|
| Your own laptop or workstation | `plexora` | [1](#1-your-own-machine-terminal) |
| Your own machine, from a notebook | `plexora.view("name")` | [2](#2-your-own-machine-jupyter) |
| A server you can `ssh` into | `plexora connect user@host` | [3](#3-a-remote-machine-over-ssh) |
| An HPC cluster with a job scheduler | `plexora connect user@host --srun "…"` | [4](#4-hpc-clusters-with-compute-nodes) |
| A hosted notebook you cannot get a terminal on | `plexora.view("name")` | [5](#5-hosted-notebooks-jupyterhub-open-ondemand-colab) |

Plus [Docker](#6-docker), [data on more than one machine](#7-data-on-more-than-one-machine),
and a [reference section](#reference) at the end.

> **A note on security, once, up front.** Plexora has no login screen and no
> user accounts. Anyone who can reach the port can read and modify every
> project in that data directory. This is why it binds to `127.0.0.1`
> (loopback — your machine only) by default, and why every remote pattern
> below uses an SSH tunnel rather than opening a port. Do not put it on a
> public interface.

---

## 1. Your own machine, terminal

### Install

```bash
conda create -n plexora python=3.13
conda activate plexora
pip install plexora
```

Then, any time you want to use it:

```bash
conda activate plexora
plexora
```

That is the whole thing. `plexora` starts the server, prints the URL, and opens
your default browser.

<details>
<summary>Installing from a source checkout instead</summary>

```bash
git clone https://github.com/nirmallab/plexora.git
cd plexora
conda create -n plexora python=3.13 && conda activate plexora   # or any venv
pip install -e ".[dev,jupyter]"
plexora
```

`pip install -e .` matters even from a checkout: the `plexora` command and the
notebook sidecar both need the package importable by name, not merely present
in the current directory. The extras are optional — `dev` adds pytest and
`jupyter` adds the notebook sidecar. Everything the app itself needs, including
vector PDF export, is a plain dependency of `plexora`.

</details>

### What happens the first time

```
$ plexora
Plexora will keep your projects in:
  C:\Users\aj\AppData\Local\plexora
Move it any time with 'plexora config set data-dir <path>'.
Serving Plexora at http://127.0.0.1:8000/
Opening browser...
```

The first line only appears once — the first time Plexora runs against a data
directory that has no projects in it yet. Read it; that directory is where all
your work will live, and it is a platform convention path you are unlikely to
guess later.

### Opening a project directly

```bash
plexora my_dataset          # skip the picker, go straight in
plexora "Tonsil 2"          # quote names with spaces
```

### Where your data lives

Plexora keeps projects, derived image pyramids, figures and settings in one
directory. It is chosen by the first rule that matches:

| Rule | Location |
|---|---|
| `--data-dir` on the command line | whatever you pass |
| `PLEXORA_DATA_PATH` in the environment | whatever you set |
| A recorded setting | whatever `plexora config set data-dir` last wrote |
| Default | `%LOCALAPPDATA%\plexora` · `~/Library/Application Support/plexora` · `~/.local/share/plexora` |

It never depends on the directory you started `plexora` from, and it is never
inside the installed package. To see which rule won:

```bash
$ plexora where
data root:    C:\Users\aj\AppData\Local\plexora
  chosen by:  platform default
shared roots: (none)
```

To move it permanently — worth doing on a laptop with a small system drive, or
anywhere a home directory has a quota, because image pyramids are large:

```bash
plexora config set data-dir /scratch/aj/plexora
plexora config show
```

For one run only, without recording anything:

```bash
plexora --data-dir /tmp/scratch-project
```

And for scripting, `plexora where --data-dir-only` prints just the path.

### Shared, read-only project directories

Several people on one workstation can share a directory of common datasets
while keeping their own work private:

```bash
plexora config set shared-dirs /srv/plexora/common
# or, for one run:
export PLEXORA_SHARED_PATH=/srv/plexora/common
plexora
```

Shared projects appear in Open Project marked *Shared*. You can open and
explore them but not edit or delete them, and everything you produce while
exploring one — gates, ROIs, figures, cached results — is written to **your**
data directory, not the shared one. A project of your own with the same name
takes precedence over the shared copy.

Multiple shared roots are separated the way `PATH` is (`;` on Windows, `:`
elsewhere), or comma-separated for `config set`:

```bash
plexora config set shared-dirs "/srv/plexora/common,/srv/plexora/published"
plexora config set shared-dirs ""     # clear them
```

### Ports

The default is 8000. If something else already has it — usually a second
Plexora in another terminal — Plexora moves aside and tells you:

```
$ plexora
Port 8000 is in use; using 51423 instead.
Serving Plexora at http://127.0.0.1:51423/
```

If you *asked* for a specific port and it is taken, that is an error rather
than a surprise, because you asked for that number for a reason:

```
$ plexora --port 9000
Port 9000 on 127.0.0.1 is already in use.
Another Plexora may already be running there -- try opening http://127.0.0.1:9000/ first.
Otherwise pass a different --port, or --port 0 to pick a free one.
```

`--port 0` always picks a free one.

### Choosing which tools load

Plexora ships four optional tools: `cell_explorer`, `figure_builder`,
`gating`, `roi`. By default **all installed tools load**.

```bash
plexora --plugins gating              # only gating
plexora --plugins gating,roi          # two of them
plexora --plugins ""                  # core only, no tools at all
```

Note the distinction: *omitting* `--plugins` means "everything installed";
passing an empty string means "deliberately nothing". They are not the same,
and Plexora is careful to keep them apart.

### Everything else

| Flag | Meaning |
|---|---|
| `--version` | Print the version and exit |
| `--no-browser` | Serve, print the URL, but do not open a browser |
| `--browser` | Open a browser even if the environment looks headless |
| `--host` | Bind address (default `127.0.0.1`; see the security note) |
| `--base-url` | Mount under a path prefix, for a reverse proxy |
| `-r` / `--remote` | Print SSH tunnel instructions — see [section 3](#3-a-remote-machine-over-ssh) |

Browser auto-open is automatic on Windows and macOS, and on Linux when a
display is present. It is skipped when the environment looks headless — CI, an
SSH session, or a scheduler job — and says so:

```
Browser auto-open skipped: headless environment detected.
```

### If `plexora` is not on your PATH

This happens on Windows, and in conda environments activated after the shell
started. Use the module form, which is identical in every respect:

```bash
python -m plexora
python -m plexora my_dataset --port 9000
```

---

## 2. Your own machine, Jupyter

### Install

```bash
conda activate plexora
pip install "plexora[jupyter]"
```

### Use

```python
import plexora

plexora.view("my_dataset")
```

Make that the last line of a cell and the viewer appears inline. Behind the
scenes Plexora starts a small server (a "sidecar") beside your kernel and shows
it in an iframe.

### Registering data from files you can see from the notebook

If the project does not exist yet, build it from paths rather than through the
import page:

```python
from plexora.jupyter import PlexoraViewer

viewer = PlexoraViewer.from_files(
    name="my_dataset",
    image="/data/tonsil.ome.tif",
    segmentation="/data/tonsil_mask.ome.tif",
    features="/data/tonsil_cells.csv",
    x="X_centroid",
    y="Y_centroid",
    id_column="CellID",
)
viewer
```

From an AnnData object or `.h5ad` file:

```python
viewer = PlexoraViewer.from_anndata(
    name="my_dataset",
    image="/data/tonsil.ome.tif",
    adata=adata,                 # or features="/data/tonsil.h5ad"
    obsm_key="spatial",
    celltype_column="phenotype",
)
viewer
```

Both accept every `PlexoraViewer` keyword as well, and both default to the
same data directory `plexora where` reports — so a project registered in a
notebook shows up in the terminal app, and vice versa.

### The viewer object

```python
viewer = plexora.view("my_dataset")

viewer.url            # the address the iframe points at
viewer.open()         # open it in a browser
viewer.iframe()       # the iframe as an IPython display object
```

Useful keywords:

| Keyword | Default | Meaning |
|---|---|---|
| `data_dir` | the resolved data root | Work against a different directory |
| `height` | `850` | Iframe height in pixels |
| `width` | `"100%"` | Iframe width |
| `plugins` | all installed | Same convention as the CLI flag |
| `proxy` | `"auto"` | See [section 5](#5-hosted-notebooks-jupyterhub-open-ondemand-colab) |
| `start` | `True` | Pass `False` to build the object without starting a server |

### One server per notebook, not one per cell

Calling `view()` a second time with the same data directory **reuses the
running sidecar** rather than starting another:

```python
a = plexora.view("tonsil")
b = plexora.view("spleen")

a.url, b.url      # same port in both — one server, two views
```

The sidecar is stopped when the kernel exits. Different data directories, or
different `plugins` settings, get their own.

### What is different in notebook mode

The server knows it is a sidecar, and two things change accordingly:

- **File → Quit is not shown.** That process belongs to your kernel; killing it
  from the browser would leave you with a viewer object whose iframe silently
  stopped loading.
- **The "Browse…" buttons are disabled.** A native file dialog would open on
  the machine running the *server*, which in a hosted notebook has no screen at
  all. Type the path into the field instead.

---

## 3. A remote machine over SSH

Your data is on a lab server or a workstation down the hall. You want to look
at it from your laptop.

### Why a tunnel

Plexora binds to loopback and has no authentication, so there is nothing to
connect to from outside — deliberately. An SSH tunnel gives you a local port on
your laptop that forwards to the remote loopback port, over the connection you
have already authenticated. Nothing is exposed to the network.

There are two ways to set one up. They do the same thing.

### Option A — let Plexora do it

Run this **on your own computer**, not on the server:

```bash
plexora connect aj@server.lab.edu
```

```
$ plexora connect aj@server.lab.edu
$ ssh -t -L 51234:127.0.0.1:51234 aj@server.lab.edu plexora --remote --no-browser --port 51234
  [ssh] [plexora-remote] node=server.lab.edu port=51234
  [ssh] Serving Plexora at http://127.0.0.1:51234/

Plexora is available at http://127.0.0.1:51234/
Leave this command running; press Ctrl+C to disconnect.
```

Your browser opens automatically. Leave the command running for as long as you
want the viewer; Ctrl+C closes the tunnel and stops the remote server.

It shells out to your system `ssh`, so whatever `~/.ssh/config`, an agent, a
ProxyJump, a hardware token or your site's Kerberos setup already do for
`ssh aj@server.lab.edu` happen here too. Nothing needs describing twice.

Open a project directly:

```bash
plexora connect aj@server.lab.edu my_dataset
```

### Option B — do it yourself

On the server:

```bash
plexora --remote
```

```
[plexora-remote] node=o2-workstation.hms.harvard.edu port=8000

Plexora is running on o2-workstation.hms.harvard.edu, bound to 127.0.0.1:8000.
From your own machine, run:
  ssh -N -L 8000:127.0.0.1:8000 aj@o2-workstation.hms.harvard.edu
then open  http://localhost:8000/
```

Paste that `ssh` line into a terminal on your laptop, leave it running, and
open the URL. `--remote` also suppresses the browser on the server side, where
opening one would be pointless.

(The `[plexora-remote]` line is for `plexora connect` to read. You can ignore
it.)

### When the remote `plexora` is not found

The most common failure, and it has a specific cause: a non-interactive SSH
session gets a shorter `PATH` than a login shell, so conda environments are
often not active. Plexora recognises it and says so:

```
The remote host could not run 'plexora':
    bash: plexora: command not found

A non-interactive ssh session often has a shorter PATH than a login shell.
Name the command explicitly, e.g.
    --remote-command "conda run -n myenv plexora"
    --remote-command /home/you/miniconda3/envs/myenv/bin/plexora
Or run `plexora --remote` on the host yourself and use the tunnel command it prints.
```

So:

```bash
plexora connect aj@server.lab.edu --remote-command "conda run -n plexora plexora"
```

`--remote-command` is spliced in as a raw shell fragment, so it can be an
expression: `"source ~/setup.sh && plexora"` works.

### `plexora connect` flags

| Flag | Meaning |
|---|---|
| `--remote-command CMD` | How to invoke Plexora on the far side (default `plexora`) |
| `--srun "ARGS"` | Run inside a SLURM job — see [section 4](#4-hpc-clusters-with-compute-nodes) |
| `--bind-node` | Forward from the login node instead of ssh-ing into the compute node |
| `-J` / `--jump HOST` | An `ssh -J` jump host on the way to the target |
| `--ssh-opt KEY=VALUE` | An extra `ssh -o` option; repeatable |
| `--port N` | Local port (default: a free one) |
| `--remote-port N` | Remote port (default: a free-looking high one) |
| `--timeout SECONDS` | How long to wait for Plexora to answer (default 60; 900 with `--srun`) |
| `--data-dir PATH` | Data directory **on the remote host** |
| `--plugins LIST` | Tools to activate **on the remote host** |
| `--no-browser` | Set the tunnel up and print the URL, but do not open a browser |

Keys, ports and usernames are usually better placed in `~/.ssh/config` than
passed as `--ssh-opt`.

---

## 4. HPC clusters with compute nodes

This is the case that is genuinely different, and it is different because
**three machines are involved, not two**:

```
your laptop  ──ssh──▶  login node  ──srun──▶  compute node
                       (no heavy work         (where Plexora
                        allowed here)          actually runs)
```

Only the login node accepts connections from outside. The compute node — the
one Plexora is on — does not, and you do not know which node you will get until
the scheduler grants it. So a single `-L` forward cannot work, and neither can
a command you write down in advance.

### Option A — let Plexora do it

Run this **on your own computer**. The target is the **login** node:

```bash
plexora connect aj@o2.hms.harvard.edu --srun "-p interactive -t 4:00:00 --mem 16G"
```

```
$ ssh -t aj@o2.hms.harvard.edu srun -p interactive -t 4:00:00 --mem 16G plexora --remote --no-browser --port 51234
  [job] srun: job 41250938 queued and waiting for resources
  waiting for the scheduler to allocate a node...
  [job] srun: job 41250938 has been allocated resources
  [job] [plexora-remote] node=compute-a-16.o2.rc.hms.harvard.edu port=51234
  Plexora is on compute-a-16.o2.rc.hms.harvard.edu:51234; opening the tunnel.
$ ssh -N -J aj@o2.hms.harvard.edu aj@compute-a-16.o2.rc.hms.harvard.edu -L 51234:127.0.0.1:51234

Plexora is available at http://127.0.0.1:51234/
Leave this command running; press Ctrl+C to disconnect and end the job.
```

Everything after `--srun` is passed to `srun` verbatim, so use whatever
partition, walltime, memory and GPU flags your site expects.

Two processes are involved because they have to be: the first holds the job
open, and the second cannot be built until the first has reported which node it
landed on. Ctrl+C tears down the tunnel and then the job — verify with `squeue`
if you like.

Queueing is normal and is not a failure. `--timeout` defaults to 900 seconds in
this mode; raise it if your partition is busy.

### Option B — do it yourself

Get an interactive job the way you normally would, then run `plexora --remote`
inside it:

```bash
ssh aj@o2.hms.harvard.edu
srun --pty -p interactive -t 4:00:00 --mem 16G bash
plexora --remote
```

Plexora reads the scheduler's environment, works out which compute node it is
on and which login node you submitted from, and prints the two-hop command for
exactly that:

```
[plexora-remote] node=compute-a-16 port=8000

Plexora is running on compute node compute-a-16, bound to 127.0.0.1:8000.
From your own machine, run:
  ssh -N -J aj@login01.o2.hms.harvard.edu aj@compute-a-16 -L 8000:127.0.0.1:8000
then open  http://localhost:8000/

If your cluster refuses ssh into a compute node, restart Plexora
with --bind-node for a login-node forward instead.
```

SLURM, PBS and LSF are all recognised.

If the scheduler does not record a submission host, you get a placeholder to
fill in — or you can supply it:

```
  ssh -N -J aj@<login-host> aj@compute-a-16 -L 8000:127.0.0.1:8000
...
Replace <login-host> with the cluster login node you ssh into (or restart with --login-host).
```

```bash
plexora --remote --login-host o2.hms.harvard.edu
```

### If your cluster refuses SSH into compute nodes

The default form uses `ssh -J`, hopping *through* the login node *into* the
compute node. That requires the cluster to allow SSH to a node where you hold a
job — true on HMS O2 (via `pam_slurm_adopt`) and at many sites, but not all.

Where it is not allowed, `--bind-node` uses a plain login-node forward instead:

```bash
plexora --remote --bind-node                                    # manual
plexora connect aj@cluster.edu --srun "-p short" --bind-node    # automatic
```

```
[plexora-remote] node=compute-a-16 port=8000

Plexora is running on compute node compute-a-16, bound to 0.0.0.0:8000.
From your own machine, run:
  ssh -N -L 8000:compute-a-16:8000 aj@login01.o2.hms.harvard.edu
then open  http://localhost:8000/

Note: --bind-node makes this port reachable from anywhere on the
cluster's internal network for as long as Plexora runs.
```

Read that last note. `--bind-node` binds all interfaces, so anyone else on the
cluster's internal network can reach an unauthenticated Plexora for as long as
your job runs. Use it when you must, not by default.

### Data directories on a cluster

Two settings are worth making once, on the cluster, before anything else:

```bash
# Your own work goes to scratch, not to a quota'd home directory.
plexora config set data-dir /n/scratch/users/a/aj/plexora

# The lab's shared reference data, read-only, merged into your project list.
plexora config set shared-dirs /n/groups/mylab/plexora
```

Derived image pyramids are large; a home directory quota is the most common way
a first attempt fails. Both settings persist, so you only do this once per
cluster account.

---

## 5. Hosted notebooks: JupyterHub, Open OnDemand, Colab

You have a notebook in a browser, and no terminal on the machine running the
kernel. The problem: your kernel is not on the same machine as your browser, so
a `127.0.0.1` address in an iframe points at *your laptop*, where nothing is
listening — and the cell renders as a blank box with no error anywhere.

**You do not have to do anything about this.** The code is the same:

```python
import plexora

plexora.view("my_dataset")
```

Plexora looks at the environment, works out what kind of notebook it is in, and
builds a URL that actually resolves.

### How it decides

First match wins:

| # | Condition | URL it uses |
|---|---|---|
| 1 | You passed `base_url=` | Whatever you said |
| 2 | You passed `proxy=False` | `http://127.0.0.1:<port>` |
| 3 | Running in Colab | The `googleusercontent.com` origin Colab proxies the port on |
| 4 | The notebook's own prefix is `/node/<host>/<port>/` — Open OnDemand | `/rnode/<host>/<port>` on the portal's origin |
| 5 | A notebook prefix is discoverable **and** the kernel looks remote | `<prefix>proxy/<port>` on the notebook's own origin |
| 6 | Otherwise | `http://127.0.0.1:<port>` |

"Looks remote" means a hub variable is set, or the kernel is inside an SSH
session or a scheduler job. Rule 6 is deliberately last and deliberately not an
error — that is plain local Jupyter, and it is also **VS Code Remote**, which
forwards the port itself and would be broken by proxying it.

### JupyterHub

Needs `jupyter-server-proxy` installed **in the environment running the Jupyter
server** — which is not necessarily the environment running your kernel. Ask
your administrator, or if you manage it yourself:

```bash
pip install jupyter-server-proxy
```

Then `plexora.view("my_dataset")` works as-is. Plexora reads the hub's
`JUPYTERHUB_SERVICE_PREFIX`, mounts the sidecar under
`/user/<you>/proxy/<port>`, and the iframe loads against the hub's own
origin — which is the origin holding your session cookie.

Plexora also registers a **Plexora tile in the JupyterLab launcher**. Clicking
it opens the full application in a browser tab, with all your tools loaded, no
notebook cell involved.

Because the URL is a path rather than a full address, `viewer.open()` prints it
rather than launching a browser:

```
Open this under your Jupyter server's address: /user/aj/proxy/51234/my_dataset
```

### Open OnDemand

Works with no configuration, no hub variables and **nothing installed** — in
particular, not `jupyter-server-proxy`. OOD runs Jupyter inside a scheduler job
with a per-job URL prefix advertised nowhere your kernel can see, so Plexora
asks the running Jupyter server which prefix it is using, recognises OnDemand's
shape, and mounts itself under the portal's own proxy.

**The two doors.** An OnDemand portal proxies a compute node two ways, and
which one an app needs depends on where that app serves from:

| Door | What it does with the path | Who it suits |
|---|---|---|
| `/node/<host>/<port>/` | forwards it **unstripped** | apps mounted under the prefix — Jupyter itself, which is started with a matching `base_url` |
| `/rnode/<host>/<port>/` | **strips** it before forwarding | apps that serve at `/` — Plexora, RStudio |

Both are stock (`node_uri` and `rnode_uri` in `ood_portal.yml`), so a site whose
Jupyter arrives through `/node/…` has the reverse proxy on and near-certainly
serves `/rnode/` too. Plexora used to try `<prefix>proxy/<port>` here, which
needs `jupyter-server-proxy` in the environment running the **Jupyter server** —
on OnDemand that is an admin-controlled software module, not something you can
`pip install`. It now goes through `/rnode/` instead and needs no extension at
all.

**Two consequences you should know about.** The portal's web front end connects
to your job over the network, so the viewer cannot sit on loopback: Plexora
binds `0.0.0.0` and says so, once, when it starts:

```
Plexora is binding 0.0.0.0:<port> so Open OnDemand can reach it from the portal;
while it runs it is reachable from the cluster network, protected by a token in
the URL below.
```

The token is that protection. It is generated per server, appears once in the
URL the iframe loads, and is exchanged for a cookie scoped to this server's own
path — so treat a copied `?token=…` link like a password, and expect a plain
403 without it. Nothing else changes: `viewer.url` still works, and the token
travels with it.

**From a JupyterLab terminal** inside the same OnDemand session, `--ood` does
the same thing for the standalone app:

```bash
plexora --ood my_dataset
```

```
Plexora is running on compute-a-16, bound to 0.0.0.0:8000 so Open OnDemand can proxy it.
Open this in the browser your OnDemand session is already in:
  https://<your-OnDemand-host>/rnode/compute-a-16/8000/my_dataset?token=Xf3q…

Replace <your-OnDemand-host> with the host the OnDemand portal itself is open at.
```

That placeholder is not laziness: a compute node has no record of which public
hostname the portal is served under, and you have it in your address bar.

If your site spells the stripping door differently, name it yourself — this is
the same escape hatch in both flows:

```python
plexora.view("my_dataset", base_url="/user/aj/")   # notebook
```

```bash
plexora --ood --base-url /whatever/my/site/uses my_dataset
```

### Google Colab

```python
!pip install plexora
import plexora
plexora.view("my_dataset")
```

Colab is the odd one out: rather than a path prefix it maps the port onto a
whole separate `https://…googleusercontent.com` subdomain. Plexora asks the
Colab frontend for that address and uses it.

That question needs a browser actually connected to the kernel, so it can fail
under "Run all", after a reconnect, or in headless execution. When it does,
Plexora falls back to Colab's own iframe helper, which resolves the port in the
frontend and needs no round trip — so the viewer still appears. Only
`viewer.url` (which has to return a string) will tell you it cannot answer, and
it says exactly that.

### Overriding the detection

```python
plexora.view("my_dataset", proxy=True)                  # always proxy
plexora.view("my_dataset", proxy=False)                 # always use 127.0.0.1
plexora.view("my_dataset", base_url="/user/aj/")         # name the prefix yourself
plexora.view("my_dataset", base_url="https://plexora.lab.edu")  # a reverse proxy you run
```

---

## 6. Docker

```bash
docker build -t plexora .
# On an ARM machine (M-series Mac): docker build --platform linux/amd64 -t plexora .

docker run --rm -dp 8000:8000 \
  -v ~/plexora-data:/app/data \
  -v /path/to/images:/data \
  plexora
```

Then open <http://localhost:8000/>.

- `-p 8000:8000` publishes the port.
- `-v ~/plexora-data:/app/data` persists your projects. Inside the image the
  data directory is pinned to `/app/data` (`PLEXORA_DATA_PATH`); without this
  volume everything is lost when the container exits.
- `-v /path/to/images:/data` makes your images visible. On the import page,
  type the container-side path (`/data/tonsil.ome.tif`), not the host one.

The image also sets `PLEXORA_HOST=0.0.0.0`, because a published port could
never reach a loopback-bound server, and `PLEXORA_DOCKER=1`, which switches the
import page to container-shaped path hints. The isolation boundary here is the
container, not the bind address.

---

## 7. Data on more than one machine

Everything above assumes one machine holds the image, the mask and the cell
table. Sometimes it does not: the slide is on cluster scratch and the `.h5ad`
came back to your laptop, or the imaging core keeps the pyramids on a
workstation nobody wants to copy 200 GB off.

A **data node** is a Plexora with the viewer switched off. It holds files and
answers questions about them; it has no project registry, no database, no
figures and no ROIs. All of that stays on the machine running the viewer, which
is the one you already use — so a node can restart, move or disappear without
anything you have made being at risk.

### Start a node where the data is

```bash
plexora node serve \
  --serve image:tumor=/scratch/me/tumor.ome.tif \
  --serve table:cells=/scratch/me/cells.h5ad
```

It prints a token and the line to run on the viewer's machine. Each `--serve`
is `kind:id=path`, where `kind` is `image`, `segmentation` or `table`, and `id`
is the name projects will point at — pick something stable, because a node that
renamed its resources on every launch would orphan every project using it.

Bound to `127.0.0.1` by default. To let another machine reach it:

```bash
plexora node serve --host 0.0.0.0 --port 8642 --serve table:cells=/data/cells.h5ad
```

The token is required either way. A loopback port on a shared machine is not
private, which is the same reasoning `--ood` follows.

### Register it, then point a project at it

In the viewer: **Settings → Data nodes → Add a node**. Name it, paste the
address and the token, and it is checked before it is saved. Then open the
project's **Edit** page: *Where the data lives* has one row per resource, and
each offers the nodes serving something of that kind right now.

The equivalent without the UI:

```python
import plexora
plexora.nodes.register_node("hpc", "http://compute-3:8642", token="…")
plexora.nodes.attach_table("tonsil", node="hpc", resource_id="cells")
plexora.nodes.attach_image("tonsil", node="hpc", resource_id="tumor")
```

### Through a tunnel

If the node runs beside the viewer on a remote host, the viewer reaches it over
there and you need nothing extra — register it as `http://127.0.0.1:8642` and
it works. Add a second forward only if your BROWSER has to reach it directly:

```bash
plexora connect me@host --forward 8642
```

### What actually crosses the network

Nothing large, and nothing twice:

| | |
|---|---|
| A table node | the cell ids and coordinates once when the project opens (~20 bytes a cell), then whole columns as a tool asks for them — the same payloads the browser already receives today |
| An image node | encoded tiles, in the viewer's own format, and a few hundred floats of statistics per channel |
| Work that must not move | the ROI-to-cell spatial join, writing annotation columns onto the cells, writing gate thresholds into `uns`, the per-channel mixture fits, a CSV export |

That last row runs **on the node**, because each of those reads the file and
the loaded table together and checks that they still agree about which row is
which cell. A check like that means nothing across a network.

### When a node is not answering

The project still opens. Whatever came from that node is absent and says so —
Settings shows the node as *Not answering*, the Edit page keeps showing the
binding, and layers that needed it are simply not drawn. Start the node again
and reload the project.

### Limits worth knowing before you plan around them

- **The viewer's machine has to be able to reach the node.** A node behind NAT
  that only your browser can see is not supported: attaching sends the read
  spec to the node and reads the table's shape back.
- **A mask has to be converted on the node before it can be served.** The masks
  a segmentation pipeline produces are one full-resolution plane, and no tile
  route can serve a zoomed-out level of that. `plexora node serve` refuses at
  startup and prints the command:

  ```bash
  plexora node prepare /scratch/me/mask.ome.tif
  ```

  A node does not do this by itself, because the converted file is often larger
  than the original and where it lands is a question about somebody's disk
  quota.
- **Bringing a resource home asks where the file is.** A table that was on a
  node has no local copy by construction, so the Edit page asks for a path
  rather than assuming one.

---

## Reference

### Environment variables

| Variable | Effect |
|---|---|
| `PLEXORA_DATA_PATH` | Data directory for this process. Highest-priority rule. |
| `PLEXORA_SHARED_PATH` | Shared read-only roots, `PATH`-separated (`;` Windows, `:` elsewhere) |
| `PLEXORA_HOST` | Default bind address, overriding `127.0.0.1` |
| `PLEXORA_PLUGINS` | Tools to activate. Unset = all installed; `""` = core only |
| `PLEXORA_BASE_URL` | Mount path prefix, for a reverse proxy |
| `PLEXORA_NOTEBOOK_MODE` | Marks the process as a notebook sidecar (set for you) |
| `PLEXORA_DOCKER` | Marks the process as containerised (set by the image) |
| `PLEXORA_AUTH_TOKEN` | Require this token to reach the server. Set for you on the Open OnDemand routes; unset everywhere else, where loopback is the boundary |
| `PLEXORA_NODE_TOKEN` | Default `--token` for `plexora node serve` |
| `PLEXORA_NODE_HOST` | Default bind address for `plexora node serve` |

### Commands

```
plexora [datasource]        start the server
plexora where               print the data directory and the rule that chose it
plexora config show         print the recorded settings
plexora config set KEY VAL  set data-dir or shared-dirs
plexora connect TARGET      from your machine: start + tunnel + open a remote Plexora
plexora node serve          serve data files to a Plexora viewer running elsewhere
plexora node prepare        convert a label mask into something a node can serve
plexora --remote            on a server: print the tunnel command to run from your machine
plexora --ood               in an Open OnDemand session: print the portal URL to open
python -m plexora …         identical to `plexora …`, for when it is not on PATH
plexora-server              the low-level sidecar the notebook and proxy spawn (not for direct use)
```

Every command takes `--help`.

### Which port am I looking at?

Three ports get mentioned in the remote patterns, and it is worth being clear:

- the **remote port** — what Plexora binds on the far machine;
- the **local port** — what the tunnel opens on your laptop;
- the **URL port** — what you type in the browser, which is always the local one.

`plexora connect` uses the same number for both ends whenever it can, precisely
so this question does not arise.

---

## Troubleshooting

**"I can't find my projects."** Run `plexora where`. If it names a directory
you did not expect, something set `PLEXORA_DATA_PATH` or a recorded setting
points elsewhere — the "chosen by" line says which. Your old projects are
probably still in the other directory; point Plexora back with
`plexora config set data-dir <path>`.

**The browser opens but the page never loads.** Check the port in the address
bar against the one Plexora printed — if 8000 was busy it moved.

**A blank box where the viewer should be, in a notebook.** The URL is pointing
somewhere your browser cannot reach. Try `plexora.view("name", proxy=True)`; if
that fixes it, the environment was not recognised as remote, and
`jupyter-server-proxy` needs to be installed on the Jupyter *server*. Plexora
now asks the server itself and says so when that is the problem, rather than
guessing from your kernel's environment.

**A 404 that is clearly *Jupyter's* error page, at a URL containing
`/proxy/`.** That is the jupyter-server-proxy route on a server that does not
have the extension. On Open OnDemand, Plexora no longer uses that door — if you
are seeing it, something pinned the old behaviour (an explicit `base_url=`, or
an older version).

**"This viewer requires a token."** You reached a token-protected server
without the token — an Open OnDemand link that lost its `?token=…`, or a
bookmark from a previous session (the token changes every time the server
starts). Re-run the cell, or re-run `plexora --ood`, and use the link it
prints.

**`plexora connect` says the remote could not run `plexora`.** A
non-interactive SSH session has a shorter `PATH`. Use
`--remote-command "conda run -n <env> plexora"`, or fall back to running
`plexora --remote` on the host yourself.

**The SSH tunnel is refused on a cluster.** Your site probably does not allow
SSH into compute nodes. Add `--bind-node` at whichever end you are using, and
read the exposure note it prints.

**The job sits in the queue forever.** That is the scheduler, not Plexora.
Check `squeue`; raise `--timeout` if the wait is expected.

**Tools are missing from the navbar.** Something passed `--plugins ""` or a
short list. Run without `--plugins` to load everything installed, and check
`pip list | grep plexora` if a tool you expect is genuinely not installed.

**"Shutdown is managed by the notebook session."** Expected — File → Quit is
disabled in notebook and hosted mode. Stop the kernel instead.

**A data node shows as "Not answering".** The node process has stopped, or the
address is not reachable from the machine running the viewer. Projects reading
from it still open; what came from it is absent. Start
`plexora node serve` again and reload the project.

**"wrong or missing node token".** The token is printed by
`plexora node serve` at startup and changes on every launch unless you pass
`--token`. Re-register the node under Settings → Data nodes with the current
one, or start the node with a fixed `--token`.

**"this node does not serve X".** The `--serve` id has to match what the
project points at. `plexora node serve` prints the ids it is serving; the
Edit page lists them too.
