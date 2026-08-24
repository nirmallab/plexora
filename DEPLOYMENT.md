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

Plus [Docker](#6-docker) and a [reference section](#reference) at the end.

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
conda env create -f requirements.yml    # creates an env named "plexora"
conda activate plexora
pip install -e .
plexora
```

`pip install -e .` matters even from a checkout: the `plexora` command and the
notebook sidecar both need the package importable by name, not merely present
in the current directory.

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
| A frozen executable | beside the executable |
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

## 1b. Clickable executables

Prebuilt, self-contained executables for Windows and macOS are published at
<https://github.com/nirmallab/plexora/releases>. No Python, no conda, no
install — download, double-click, and a browser opens.

A frozen build keeps its data **beside the executable**, so the whole thing
can be moved to another folder or handed over on a USB stick as one unit. If
you would rather it used a fixed location, `plexora config set data-dir` works
from a frozen build too.

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
| 4 | A notebook prefix is discoverable **and** the kernel looks remote | `<prefix>proxy/<port>` on the notebook's own origin |
| 5 | Otherwise | `http://127.0.0.1:<port>` |

"Looks remote" means a hub variable is set, or the kernel is inside an SSH
session or a scheduler job. Rule 5 is deliberately last and deliberately not an
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

Works with no configuration and no hub variables. OOD runs Jupyter inside a
scheduler job with a per-job URL prefix that is advertised nowhere your kernel
can see, so Plexora asks the running Jupyter server directly which prefix it is
using, and detects the remoteness from the job's own environment variables.

If your site's OOD image does not include `jupyter-server-proxy`, Plexora
prints a hint rather than failing:

```
Note: jupyter-server-proxy does not appear to be installed in this environment.
If the viewer below does not load, install it in the environment running your
Jupyter server:
    pip install jupyter-server-proxy
```

It is only ever a hint — on some hubs the kernel and the server are different
environments, so its absence *here* says nothing about *there*.

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

### Commands

```
plexora [datasource]        start the server
plexora where               print the data directory and the rule that chose it
plexora config show         print the recorded settings
plexora config set KEY VAL  set data-dir or shared-dirs
plexora connect TARGET      from your machine: start + tunnel + open a remote Plexora
plexora --remote            on a server: print the tunnel command to run from your machine
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
`jupyter-server-proxy` needs to be installed on the Jupyter *server*.

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
