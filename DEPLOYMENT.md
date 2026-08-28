# Running Plexora

Plexora is a local web application: a Python server you start yourself, and a
browser pointed at it. Everything in this guide is a variation on that one
sentence — the only thing that ever changes is **where the server runs** and
**how your browser reaches it**.

> **Looking for step-by-step instructions rather than reference material?**
> [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md) covers the same ground written
> for someone who does not want to learn about tunnels, with a screenshot for
> each screen and a scenario-by-interface compatibility matrix. This file is
> the technical companion to it: every flag, every variable, and why each
> decision is the way it is.

There are five places it can run. Find yours:

| Where the data and the compute are | What you run | Section |
|---|---|---|
| Your own laptop or workstation | `plexora` | [1](#1-your-own-machine-terminal) |
| Your own machine, from a notebook | `plexora.view("name")` | [2](#2-your-own-machine-jupyter) |
| A server you can `ssh` into | Settings → Remote servers, or `plexora connect user@host` | [3](#3-a-remote-machine-over-ssh) |
| An HPC cluster with a job scheduler | the same, plus `--srun "…"` | [4](#4-hpc-clusters-with-compute-nodes) |
| A hosted notebook or an HPC terminal | `plexora` — it works out where it is | [5](#5-hosted-notebooks-jupyterhub-open-ondemand-colab) |

Plus [Docker](#6-docker), [data on more than one machine](#7-data-on-more-than-one-machine),
and a [reference section](#reference) at the end.

> **Two things changed recently and are worth knowing before you read on.**
>
> 1. **A bare `plexora` now works out its own environment.** Run it in a
>    JupyterHub terminal, an Open OnDemand session, or over SSH, and it
>    configures the URL to match and prints one that works from where you are
>    sitting. Every flag below still overrides it, and `--no-detect` turns it
>    off entirely. See [§5](#5-hosted-notebooks-jupyterhub-open-ondemand-colab).
> 2. **Remote servers can be saved and reconnected from inside the app.**
>    **Settings → Remote servers** does what `plexora connect` does, including
>    relaying a password or 2FA prompt to the page. Both read one
>    `remotes.json`, so a server saved either way is available to the other.
>    See [§3](#3-a-remote-machine-over-ssh).

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

**One thing is written outside it.** A segmentation mask has to be converted
into a tiled pyramid before the viewer can draw it, and that pyramid is written
next to the mask it came from — `mask.ome.tif` gets a
`mask.labels.pyramid.ome.tiff` beside it. Importing the same mask into a second
project then costs nothing, and a data node pointed at that mask finds the
conversion already done, neither of which is possible for a file filed under one
project's name. If the mask's own directory is read-only — pipeline output on a
cluster usually is — the pyramid goes into the data root above instead.

These files are yours, and deleting a project leaves them alone. Deleting one by
hand is safe: the next open rebuilds it.

To keep them out of your data folders entirely — worth it if your masks live in
a synced folder like Dropbox, or one that is backed up, since the pyramids are
large:

```bash
plexora config set mask-output project    # under the project, as before
plexora config set mask-output beside     # next to the mask (the default)
```

Both places are searched whichever you choose, so changing your mind costs
nothing: an existing pyramid is adopted wherever it is, never rebuilt because
the setting moved.

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

There are three ways to set one up. They do the same thing.

### Option A — from inside Plexora

Start Plexora on your own computer, open **Settings → Remote servers**, and
save a server. Two fields are required — a **name** you choose and the
**address** you would type after `ssh`. Then press **Connect**, and
**Open remote Plexora** when it turns green.

Everything under Option B happens, driven by the local app rather than by a
terminal: it picks the ports, spawns the ssh processes, waits for the remote
Plexora to answer through the tunnel, and hands you a link. Progress is
reported as a state rather than a spinner — `connecting`, `authenticating`,
`waiting_for_job`, `tunneling`, `connected` — because those are different
waits with different causes, and the longest of them (a scheduler queue) is
not a problem.

**Passwords, 2FA and host-key prompts.** A server that asks for a secret has
nowhere to ask when the ssh is a child of a web server, which used to make this
route unusable at password sites. It now works: the ssh is spawned with no
controlling terminal and `SSH_ASKPASS` pointing at a helper
(`plexora/askpass.py`), which posts the prompt back to the local Plexora over
loopback — authenticated by a one-time per-session nonce — and long-polls for
the answer the user types into the page. The prompt text is reproduced
verbatim, because only the user can tell a Duo push from a passphrase.

The secret is held in one attribute of one session object, handed to ssh
exactly once, and dropped. It is not in `remotes.json` (which has no field for
one), not in the status payload, and not in the served log tail — which is
additionally redacted, because a data node's announce line carries a token.

> Requires OpenSSH ≥ 8.4 for `SSH_ASKPASS_REQUIRE=force`; `DISPLAY` is also set
> as the older trigger. Reliable on macOS and Linux, not on Windows OpenSSH —
> use a key there, or `plexora connect` in a terminal.

Saved servers live in `<data_root>/remotes.json`, written 0600 by the same
writer `nodes.json` uses (`server/models/secret_store.py` — chmod before
rename, never after). Re-saving a name updates it.

### Option B — one command on your own computer

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

**Save it, and stop typing it.** `--save NAME` records the whole invocation in
the same `remotes.json` the Settings page reads:

```bash
plexora connect aj@server.lab.edu --srun "-p interactive" \
    --remote-command "conda run -n imaging plexora" --save lab

plexora connect lab                 # everything above, by name
plexora connect lab other-study     # …with a different project today
```

A bare word is looked up as a saved name first and used as a hostname if it is
not one, so nothing that worked before changes. A flag you type always beats
the saved value — the case that decides this is "same server, different project,
every day". `srun` is deliberately three-valued in the store: `None` means no
scheduler, `""` means `srun` with the site's defaults, and a string means those
arguments.

### Option C — do it yourself

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
Name the environment Plexora is installed in -- the prefix `conda env list`
prints is enough:
    --remote-command /home/you/miniconda3/envs/myenv
    --remote-command "conda run --no-capture-output -n myenv plexora"
Or run `plexora --remote` on the host yourself and use the tunnel command it prints.
```

So:

```bash
plexora connect aj@server.lab.edu --remote-command /home/aj/miniconda3/envs/plexora
```

The environment directory is enough: a path with no `bin/plexora` on the end is
read as a prefix, and the entry point inside it is filled in. That is the form
worth using, because it is the one you can look up — `conda env list` prints
prefixes and nothing prints the path to the entry point.

`--remote-command` is otherwise spliced in as a raw shell fragment, so it can
be an expression: `"source ~/setup.sh && plexora"` works. If you write a
`conda run` form yourself, include `--no-capture-output` — without it conda
buffers the child's output until it exits, and the line Plexora is waiting for
never arrives.

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
| `--save NAME` | Record this connection, so `plexora connect NAME` repeats it |
| `--also-serve KIND:ID=PATH` | Serve a file on the **remote** host as a data node beside the viewer, and register it — see [section 7](#7-data-on-more-than-one-machine) |
| `--local-serve KIND:ID=PATH` | Serve a file on **this** machine to the remote viewer, over a reverse forward |
| `--node-name NAME` | What to call the data nodes this connection registers |
| `--node-port N` | Port for the remote data node |
| `--forward [LOCAL:]REMOTE` | Forward another remote port, for a node you started yourself |

Keys, ports and usernames are usually better placed in `~/.ssh/config` than
passed as `--ssh-opt`.

The target may be a saved name instead of `[user@]host`; see
[Option B](#option-b--one-command-on-your-own-computer).

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

### …and the same is now true of a terminal

The ladder below used to be reachable only from `plexora.view()`. A user in a
JupyterHub terminal running plain `plexora` got a `127.0.0.1` URL, which is not
so much a bad address as a false one: it names their own laptop, where nothing
is listening. They had to already know to type `--base-url /user/me/`.

A bare `plexora` now asks the same question and fills in the flags it implies:

| Verdict | What the bare `plexora` does |
|---|---|
| `ood` | Behaves as `--ood`: binds `0.0.0.0`, mounts under `/rnode/<host>/<port>`, generates a token, prints the portal link. The host spelling comes from the notebook's own prefix rather than from the scheduler, because that is the spelling the portal routes. |
| `proxy` | Sets `--base-url <prefix>proxy/<port>` once the port is known, prints the path to paste after the hub's hostname, and asks the notebook server whether it will really proxy that port — so a missing `jupyter-server-proxy` is a sentence instead of a 404. |
| `colab` | Prints "run `plexora.view()` in a cell" and serves loopback. A shell has no front-end to ask for Colab's proxy origin, so this is the one case the CLI genuinely cannot finish. |
| `direct` + a remote-looking environment | Behaves as `--remote`: prints the tunnel command with the real host, user and port already in it. |
| `direct`, local | Unchanged. |

Detection is skipped entirely if any of `--ood`, `--remote`, `--bind-node`,
`--base-url`, `--host` or `--login-host` was given, if `PLEXORA_HOST` is set
(the Docker image sets it and means it), or if `--no-detect` was passed. It is
also skipped, silently, if anything in it raises — the plain local viewer has
to keep working on a machine with no Jupyter and a half-installed environment.

A detected proxy, Colab or remote verdict also suppresses the browser
auto-open, for the same reason `--remote` does: the machine with the screen is
somewhere else.

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
address and the token, and it is checked before it is saved.

For a **new** project, the import screen now offers whatever the registered
nodes are serving, under the Image and Segmentation Mask fields. Pick one and
leave the Data field pointing at your local table — that is the ordinary split,
and it is one screen. (The fields also accept `node://<node>/<resource>` typed
by hand.)

For a project that **already exists**, open its **Edit** page: *Where the data
lives* has one row per resource, and each offers the nodes serving something of
that kind right now.

The equivalent without the UI:

```python
import plexora
plexora.nodes.register_node("hpc", "http://compute-3:8642", token="…")
plexora.nodes.attach_table("tonsil", node="hpc", resource_id="cells")
plexora.nodes.attach_image("tonsil", node="hpc", resource_id="tumor")
```

### Setting it up in one action

The manual sequence above is the general case; three specific arrangements
cover almost everything and each is one command, with the node started,
forwarded, and registered without anything being copied by hand.

**1. Viewer and node both on the remote host** (the images are big and the
viewer belongs beside them):

```bash
plexora connect me@hpc --also-serve table:cells=/scratch/me/cells.h5ad
```

The remote `plexora` starts `plexora node serve` as a managed child process and
relays its output, so the announce line travels back down the ssh pipe. The
local side picks the node's remote port up front — an `-L` forward is fixed
when the connection opens, before the far side has run anything — adds a second
forward for the browser, and registers the node by POSTing to the remote
viewer's own `/settings/nodes` **through the tunnel**. `endpoint` is the far
side's loopback (viewer and node are one machine); `browser_endpoint` is this
end of the tunnel.

**2. Viewer here, data over there** (only the pixels cross the network):

```bash
plexora node connect me@hpc --serve image:tonsil=/scratch/me/tonsil.ome.tif --name hpc
```

The remote command *is* `plexora node serve`; the registration goes into this
machine's own `nodes.json`. Readiness is a node health poll rather than the
announce, because the announce is printed **before** waitress binds and a raw
mask is converted first — which can take minutes.

**3. Viewer over there, data here** (the slide never leaves the cluster and the
`.h5ad` never leaves your laptop):

```bash
plexora connect me@hpc --local-serve table:cells=~/study/cells.h5ad
```

This spawns a node on **this** machine and opens an `ssh -R` reverse forward, so
the remote viewer reaches back down the connection you already authenticated.
It is what removes the NAT limitation noted below for this case: a compute node
cannot open a connection to a laptop, but the laptop's own ssh session can lend
it one. The browser, being on the laptop too, reads that node directly — the
fastest path of any of the three.

All three are also profile fields (**Settings → Remote servers → Advanced**),
so the in-app Connect does the same thing. Reconnecting is free: the port and
token change every session and re-registering a name updates it, so projects
never need touching. Nodes registered this way are marked `managed_by` and the
Data nodes page says so rather than inviting somebody to repair an address that
is rewritten every session.

**No token is ever put on a command line.** Everything in a remote command is
visible in `ps` to every other account on a shared login node, so the node
generates its own token and prints it on stdout — inside the ssh channel — and
the registration that uses it is POSTed through the tunnel. Ports are on argv;
secrets are not. The served log tail is redacted for the same reason.

### Through a tunnel, by hand

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

The project still opens. Whatever came from that node is absent — and now says
so in the viewer as well: a dismissible banner across the top of the page names
the resource, the node and the reason, and links to Settings
(`client/src/js/services/resourceStatus.js`, fed by `/resource_status`, which
had no consumer before). Settings shows the node as *Not answering*, and the
Edit page keeps showing the binding. Start the node again and reload.

Dismissal is remembered per project for the tab, so somebody who knows their
laptop node is off and is working on the images anyway is not told again on
every navigation. A node that is merely *slow* — reachable through the primary
but not directly from the browser — never raises a banner of its own; it is a
footnote on one that already exists, because nothing is missing.

### Limits worth knowing before you plan around them

- **The viewer's machine has to be able to reach the node.** A node behind NAT
  that only your browser can see is not supported: attaching sends the read
  spec to the node and reads the table's shape back. `--local-serve` is the
  supported way to get the same effect — it gives the viewer a reverse-forwarded
  route back to the node rather than asking it to dial one it cannot.
- **An Open OnDemand `browser_endpoint` is still typed by hand.** Nothing on
  either machine records the portal's public address, so the one field that
  cannot be worked out is the one naming it (`/rnode/compute-3/8642/`).
- **A mask is converted before it is served, and the node does that itself.**
  The masks a segmentation pipeline produces are one full-resolution plane, and
  no tile route can serve a zoomed-out level of that. `plexora node serve`
  converts one at startup and starts when it finishes, writing the pyramid
  beside the mask. Later starts find that file and adopt it, so this is paid
  for once.

  Run it ahead of time to get the wait over with, or to choose where it lands:

  ```bash
  plexora node prepare /scratch/me/mask.ome.tif
  ```

  The one case it refuses is a mask in a directory it cannot write to — the
  converted file is often larger than the original, so where it goes instead is
  a question about somebody's disk quota. Name a destination and serve that:

  ```bash
  plexora node prepare /reference/mask.ome.tif /scratch/me/mask.labels.pyramid.ome.tiff
  ```
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
| `PLEXORA_MASK_OUTPUT` | `beside` (default) or `project` — where a converted segmentation mask is written. Same choice as `plexora config set mask-output`, for one run |

### Commands

```
plexora [datasource]        start the server
plexora where               print the data directory and the rule that chose it
plexora config show         print the recorded settings
plexora config set KEY VAL  set data-dir, shared-dirs or mask-output
plexora connect TARGET      from your machine: start + tunnel + open a remote Plexora
plexora connect NAME        the same, for a server saved with --save or in Settings
plexora connect T --save N  do it and remember it as N
plexora node serve          serve data files to a Plexora viewer running elsewhere
plexora node connect TARGET start a node on another machine and register it here
plexora node prepare        convert a label mask into something a node can serve
plexora --remote            on a server: print the tunnel command to run from your machine
plexora --ood               in an Open OnDemand session: print the portal URL to open
plexora --no-detect         do not work out the environment; serve plain localhost
plexora --also-serve K:I=P  run a data node beside this viewer, serving one file
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
