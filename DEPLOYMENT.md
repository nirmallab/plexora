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
| Images in a Google Cloud Storage bucket | Settings → Add a server → Google Cloud | [5b](#5b-google-cloud-a-bucket-and-a-vm-rented-to-read-it) |

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

**You type it once, not once per hop.** A scheduler connection is three ssh
authentications — the job, the login node again as a jump host, then the
compute node — so a password site used to ask the same question three times
for one press of Connect. A repeatable answer is now kept for the length of
establishment and given to the hops that follow. Only a password or a key
passphrase is ever replayed: a Duo push, a one-time code and a host-key
`(yes/no)` are asked every time, as is any prompt whose wording Plexora does
not recognise. And one ssh asking the same question twice means the answer was
refused, so it is dropped and you are asked — a mistyped password costs one
retry, not one per hop. The moment the connection is up, or has failed,
nothing is held any more.

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

## 5b. Google Cloud: a bucket, and a VM rented to read it

Every other section here starts from a machine you have. This one starts from
**data you have** — images in a Cloud Storage bucket — and treats the machine
as something Plexora asks Google for, uses, and gives back.

**Settings → Add a new server → Google Cloud (GCP)**, or the same
preset from any data field's machine picker.

### The form: four pages

The questions are asked in the order the answers depend on each other, one page
at a time, with **Next** and **Back**. Going back loses nothing, and the strip
across the top jumps to any page you have already reached.

**Google Cloud → Data → Compute → When Plexora exits → Create & Connect**

| Page | What it asks |
|---|---|
| **Google Cloud** | Sign-in, a name for the connection, and the project. Plexora authenticates through the `gcloud` CLI on your own computer — it never sees your password, and the credential stays in gcloud's store. No CLI installed says where to get it and stops there. |
| **Data** | The bucket, and where to mount it on the VM (`~/plexora-data`). The bucket is **required** — it is the reason the VM is being asked for. |
| **Compute** | A new VM or one you already run; then machine type, Spot or Standard, and the region and zone. Advanced holds boot disk, public IP, install Plexora, idle shutdown, service account and the launch command. |
| **When Plexora exits** | Leave the VM running, stop it, or delete it. |

### What it does, in order

| Step | What happens |
|---|---|
| Identity | Reads `gcloud auth list`. Not signed in offers a button that runs `gcloud auth login` in your browser. |
| Project | `gcloud projects list`. The last one you used is preselected. |
| Data | `gcloud storage buckets list`, as a dropdown showing each bucket's location. Listing is its own permission, so "Another bucket — type its name…" is there for one the list could not cover — including a **public** bucket, which Plexora checks by listing one object when it is refused the bucket's metadata. A public bucket has no readable location, so pick the region yourself on the next page. |
| Region | Taken from the bucket's location. A manual mismatch warns, with a one-press fix. |
| Compute | `e2-highmem-16` (16 vCPU, 128 GB) by default, OS Login on, Debian 13, 20 GB `pd-balanced` boot disk, tagged `plexora`. Eight types on the list, from `e2-medium` up to `n2-highmem-32`, and "Custom — type a machine type…" takes anything Compute Engine accepts (a GPU type, C3, `custom-4-8192`). |
| Provisioning | **Spot by default** — see [Spot VMs](#spot-vms-and-why-they-are-the-default). |
| Network | The VM gets an ephemeral public address **and** a firewall rule that refuses every inbound packet to it except Google's IAP range. The address is a way out, not a way in — see [Why the VM has a public IP](#why-the-vm-has-a-public-ip-address). |
| Ready | Two waits, not one: until sshd answers, then until the first-boot script has *finished*. A VM answers ssh seconds after boot and is still running `apt-get` minutes later, and on a small machine that install is most of what it has. |
| Connect | Reuse a running VM, start a stopped one, or create one — then `gcloud compute ssh --tunnel-through-iap`. **Install Plexora is ON** for this preset (it is off for every other one): the VM is Plexora's own, so each connection runs the current release rather than whatever the first boot installed. Turn it off under Advanced. |
| Mount | Cloud Storage FUSE mounts the bucket at `~/plexora-data`, which becomes Plexora's `--data-dir`. |
| Exit | Whatever the last page asked for — see [When Plexora exits](#when-plexora-exits). |

Everything after that is an ordinary Plexora connection: same tunnel, same
log, same steps, same `remotes.json` entry.

### Or a VM you already run

On the **Compute** page choose **Use an existing VM**, then pick the instance
from the dropdown of everything in the project — each listed as
`name — machine type — zone — status` — or type a name the list did not
cover. Plexora then does only
the last two rows of that table: it connects through IAP, mounts the bucket, and
launches. It does not create the machine, and — this is the point of the setting
— it will not stop or delete it either. A stopped one *is* started, because that
is what pressing Connect asked for.

**The name is enough.** You do not have to know the zone: Plexora finds the VM
across the project and takes the zone from the instance, along with the region.
That is the opposite of the rented path, where the bucket picks the region and
the region picks the zone — your machine is already somewhere, and where it is
is a fact rather than a preference. If it turns out to be far from your bucket
you get the usual amber warning about egress, phrased as a fact rather than as
something to fix, since nothing here can move a running VM.

| | Plexora starts one | You already run one |
|---|---|---|
| Missing VM | Created | An error naming it. Never created |
| Machine type, Spot/Standard, boot disk | Asked for | Not asked — not Plexora's to choose |
| Public IP | Asked for | Not asked — its network is yours |
| Region and zone | Chosen from the bucket | Read off the VM and reported |
| When Plexora exits | Leave, stop or delete | Leave or stop. **Delete is greyed out** |
| Idle self-shutdown | Installed at first boot | Never installed |
| **Delete VM…** on the card | Offered | **Refused, and the button is not drawn** |

Stopping one you already run *is* offered, because that is you answering a
question about your own machine on the form in front of you. Deleting one is
not on the menu at any price.

The refusal is checked twice: once on the saved profile, and once inside
`delete_instance`, which describes the instance and deletes only if it carries
the `created-by=plexora` label its own `create_instance` wrote. A hand-edited
profile does not get past the second check.

Two things a machine Plexora built gets for free, that yours may need:

- **gcsfuse.** Plexora tries to install it (Google's apt repo, via `sudo -n`).
  Without passwordless sudo the connection stops with the one-line installer to
  run once by hand.
- **A storage scope.** gcsfuse authenticates as the VM. An instance created
  with `storage-ro` or no storage scope gets a 403 no matter how the bucket's
  IAM reads; Plexora says so before mounting. The fix is on the instance —
  stop it, set access to "Allow full access to all Cloud APIs", start it.

### What you need, once

```bash
# On your own computer, not on the VM:
gcloud auth login
gcloud config set project YOUR_PROJECT
gcloud services enable compute.googleapis.com --project YOUR_PROJECT
```

IAM roles on the project:

| Role | Why |
|---|---|
| `roles/compute.instanceAdmin.v1` | Create, start, stop and delete the VM |
| `roles/compute.securityAdmin` | Write the two firewall rules below. Without it Plexora prints them for an administrator and carries on |
| `roles/iap.tunnelResourceAccessor` | Reach the VM through the tunnel |
| `roles/iam.serviceAccountUser` | Attach the compute service account to the VM |
| `roles/storage.objectViewer` on the bucket | Read your images (`objectUser` to save figures into it) |

The VM authenticates to Cloud Storage **as itself**, through the metadata
server — no key file is copied to it and none is stored by Plexora. If the
bucket is in a different project from the VM, grant the VM's service account
access to it:

```bash
gcloud storage buckets add-iam-policy-binding gs://BUCKET \
  --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com \
  --role=roles/storage.objectUser
```

### The two firewall rules

IAP connects from `35.235.240.0/20`, and that range needs to reach port 22.
Plexora looks for a rule that allows it and creates `plexora-allow-iap-ssh` if
there is none. The second rule closes everything else. Both are written before
the VM exists, and on a network where Plexora is not allowed to write them it
says so and prints them for whoever administers the project:

```bash
gcloud compute firewall-rules create plexora-allow-iap-ssh \
  --project PROJECT --direction=INGRESS --action=allow \
  --rules=tcp:22 --source-ranges=35.235.240.0/20

gcloud compute firewall-rules create plexora-deny-public-ingress \
  --project PROJECT --direction=INGRESS --action=deny \
  --rules=all --source-ranges=0.0.0.0/0 \
  --target-tags=plexora --priority=65000
```

The deny rule is scoped to the `plexora` network tag, so the strongest thing it
can do is cut a machine Plexora created off from inbound traffic; nothing else
in the project is affected by it. Priority 65000 puts it under the default
VPC's own permissive rules (`default-allow-ssh` is 65534, and open to the whole
internet) and far above the 1000 a deliberate rule gets, so it overrides what a
project arrives with and never overrides a decision somebody made.

### Why the VM has a public IP address

Because a VM with no address cannot install anything, and Plexora's first
connection has to install two things: Cloud Storage FUSE from
`packages.cloud.google.com`, and Plexora itself from PyPI.

A Compute Engine VM created with `--no-address` has **no outbound route at
all** unless the network provides one. On a default VPC subnet it does not:
`privateIpGoogleAccess` is off, and there is no Cloud Router. IAP still reaches
*in* — that is a separate path — so the machine boots, answers the tunnel and
looks perfectly healthy while `apt-get` times out against every mirror.

There are two ways to give it a route out:

| | Cost | Reaches |
|---|---|---|
| **Public IP on the instance** (default) | Free while the VM runs; the VM is stopped when you disconnect | Everything |
| **Cloud NAT** in the region | ~$32/month for the gateway, plus $0.045/GB | Everything |
| *(Private Google Access alone)* | Free | Google domains only — **not PyPI**, so Plexora will not install |

For a preset whose machine is stopped between sessions, a gateway that bills
every hour of every month whether or not a VM exists is not
a sensible default, so the VM gets an address and `plexora-deny-public-ingress`
takes back what the address gives away. If your project already has Cloud NAT
in the VM's region, turn **Advanced → Give the VM a public IP address** off and
Plexora will use `--no-address` instead.

With that switch off and no NAT, Plexora refuses **before creating anything**
and prints the two commands that set NAT up — the failure it is preventing
otherwise costs a VM, a disk and eight minutes of watching a progress bar.

A VM created by an older Plexora is repaired on the next connection: it is
tagged first and given an address second, in that order, so it is never
addressable before the deny rule covers it. A VM you already run is never
tagged and never given an address — its network is your decision.

### Cost, and the three ways to finish

A running VM is billed whether or not anybody is connected — an `e2-highmem-16`
is roughly **$0.53/hour, about $385/month** left running. So a VM Plexora
rents is given back by default.

| Button | What it costs afterwards | What survives |
|---|---|---|
| Disconnect | The boot disk only (~$2/mo at 20 GB) | The disk, the environment on it, the bucket |
| Stop VM | The same | The same |
| Delete VM… | Nothing | **The bucket and everything in it** |
| Forget | Whatever the VM was already costing | The VM. Delete it first if you are done |

The card shows what the machine is doing — `VM running`, `VM stopped`,
`no VM yet` — and offers **Start VM** or **Stop VM** accordingly, so a stopped
profile can be warmed up before you need it rather than only as a side effect of
connecting. That status is read once per card and again after anything you press
that could change it; it is deliberately **not** part of the once-a-second poll,
which would be a `gcloud` subprocess per cloud profile per second.

<a id="when-plexora-exits"></a>

### When Plexora exits

The last page of the form is one question with three answers, and it is the
setting that decides what a session costs *after* it is over.

| | What it costs afterwards | What it costs you |
|---|---|---|
| **Leave VM running** | Compute, by the hour, including overnight | Nothing — reconnecting is instant |
| **Stop VM** (default) | The boot disk, ~$2/month at 20 GB | Under a minute on the next connection |
| **Delete VM** | Nothing at all | A few minutes on the next connection, while a new machine is built |

**Stop** is the default because it is wrong in the cheapest direction: leaving
a 16-core machine running is a bill that grows while nobody is looking, and
deleting one costs a rebuild that is noticed immediately.

**Delete VM** deletes the VM and its boot disk. It does not touch the bucket,
and it does not touch the saved connection — connecting again simply builds a
new machine against the same data. It is not offered at all for a VM you
already run.

**Every way a session can end honours that setting**, not only the Disconnect
button:

| How it ended | What happens |
|---|---|
| Disconnect | What the form asked for |
| The connection failed after the VM came up | **Stopped** — even when the setting says Delete, and even when it says Leave. A machine that never carried a session is not something to keep paying for, and its disk holds the two logs that explain why the connection failed. Only if *this attempt* created or started it; one that was already running is left alone |
| The connection died on its own | What the form asked for |
| Plexora quit, or was Ctrl-C'd | What the form asked for (`atexit`) |
| The laptop died, lost power, or was SIGKILLed | **The VM shuts itself down**, ~30 minutes after the last ssh session ends |

That last row is the only one nothing on your computer can do anything about,
which is why it runs on the VM: a systemd timer installed at first boot, checking
every five minutes for any ssh session at all. Plexora holds one open for the
life of a connection, so a long analysis with the browser closed still counts as
busy — the clock only starts once the last session has genuinely gone. Set the
window under **Advanced → Idle shutdown time**, or `0` to switch it off. That
timer stops the VM; it does not delete one, even for a profile set to Delete,
because nothing on the machine can tell the difference between a laptop that
died and one that is about to reconnect.

A **stopped** VM is not free: it still bills for its boot disk, roughly $2 a
month at the default 20 GB, until you delete it. Both the Delete confirmation
and the Forget confirmation say so.

### Why the boot disk is 20 GB

Almost nothing of yours is on it. The images stay in the bucket, and a data-node
connection keeps your projects and databases on your own machine. What the disk
holds is the Debian image (~2 GB), `~/plexora-venv` (~1.5 GB across some thirty
thousand files — scipy, scikit-image, scikit-learn, polars, pyarrow,
spatialdata, imagecodecs), pip's cache while that installs, and **gcsfuse's
staging area**: writing an object to the bucket stages the whole of it on local
disk first, at `~/.plexora-gcsfuse-tmp`.

So ~5 GB is in use and the rest is room to write, on a disk that goes on
billing after the VM stops and stays there until somebody deletes it. The
floor is Google's own — a boot disk may not be smaller than the image it is
built from, and the Debian cloud image is 10 GB.

What 20 GB trades away is speed, in one specific place: on `pd-balanced`
**Google sells throughput and IOPS by the gigabyte** — 6 IOPS and 0.28 MB/s
per GB — so 20 GB is 120 IOPS, and the first connection's job is unpacking
thirty thousand files. **The first connection to a new VM is therefore slower
than it would be on a larger disk, and every connection after it is
unaffected**, because the venv is already there.

Raise it under **Advanced → Boot disk** if a session writes very large derived
images back to the bucket, or if you build new VMs often enough that the first
install's speed matters.

Two gigabytes of it are a swap file, created at first boot. A Debian cloud
image has none, and the smallest machine this preset offers is a fraction of
two cores with 4 GB of RAM — so asking pip to resolve and unpack that
dependency list there can be an OOM kill with nothing in the log to explain it.
The swap makes the smallest tier slow rather than uncertain.

### The image, and why it is Debian 13

Plexora's `requires-python` is `>=3.12,<3.14`. Debian 12 (bookworm) ships
Python 3.11, so a VM built from it mounts the bucket perfectly and then fails
the last step with:

```
ERROR: Ignored the following versions that require a different python version:
  0.0.12 Requires-Python <3.14,>=3.12
ERROR: No matching distribution found for plexora
```

— which reads as *"this package does not exist"* rather than *"this machine is
too old"*. Debian 13 (trixie) ships Python 3.13, so `IMAGE_FAMILY` is
`debian-13`. The prep chain now checks the version before building the venv and
says which it is in one sentence.

**An image family only applies to a VM being created.** A VM built before this
change keeps its old image forever, so a machine stuck on Python 3.11 has to be
deleted — **Delete VM…** in Settings — and rebuilt by the next connect. That
costs a minute and nothing else; the bucket is untouched.

Neither the startup script nor the fallback names an apt suite any more: both
read `VERSION_CODENAME` from `/etc/os-release` on the machine itself, so
changing the image family cannot silently point apt at a repository for a
different Debian.

Nor do they run the key through `gpg --dearmor`. Google publishes it
ASCII-armoured and apt accepts an armoured key in `signed-by` as it is, so the
conversion added a dependency on a program **the Debian 13 image does not
ship** — and the only visible symptom was `E: Unable to locate package
gcsfuse`, four steps downstream of the real line (`gpg: not found` → no
keyring → repository rejected as unsigned → package invisible). The key is now
fetched straight to `/usr/share/keyrings/cloud.google.asc`.

If a connection ever fails at this step again, the error carries the last 25
lines of both `/var/log/plexora-startup.log` and
`/tmp/plexora-gcsfuse-install.log` from the VM itself. That is the first place
to look — not `gcloud`, whose serial console only answers while the instance
is running, and Plexora stops an instance whose connection failed.

<a id="spot-vms-and-why-they-are-the-default"></a>

### Spot VMs, and why they are the default

A Spot VM is the same hardware as a Standard one, usually 60–91% cheaper, on
one condition: Compute Engine may reclaim it at any time, with about thirty
seconds' notice, when somebody paying full price wants the capacity.

For a server that has to stay up, that is a serious risk. For this preset it is
an **interruption**, and the difference is where the data is. Your images are in
the bucket, not on the machine — and Plexora asks for a preempted VM to be
*stopped* rather than deleted (`--instance-termination-action=STOP`), so the
boot disk with `~/plexora-venv` on it survives. Being reclaimed costs a
reconnect, which starts the same machine again in under a minute.

Choose **Standard** on the Compute page for a long import you are not watching,
a demo, or anything else that must not be interrupted.

**When a zone has no spare Spot capacity**, the failure carries a
**Reconnect with Standard** button beside Try again. Pressing it changes the
saved profile to Standard and connects again — same zone, same machine type,
same bucket, bought outright. Everything about the request was right except the
price, and sending somebody round four zones for a machine that would have been
created immediately at full price is a bad half-hour.

The change is to the *saved* profile, deliberately: a record saying Spot while
the machine it describes was bought outright would be wrong on the Settings
card, wrong in the form and wrong on the next create. The card prints
`standard` afterwards, so the new price is visible where the machine is.

That button appears only where `plexora.gcloud` attached a recovery key to the
failure — the browser never infers one from the error text. The identical
words about capacity on a *Standard* request mean the zone genuinely has none,
and there the answer really is another zone.

### A word about the smallest machine type

`e2-medium` is on the list so that checking the connection works does not cost
128 GB of RAM for a few minutes. It is not a machine to *work* on: 4 GB of RAM
and a **fraction** of two vCPUs that burst, so everything is slow, including
the apt install at first boot and the `pip install` after it. Expect a first
connection there to take many minutes.

If you are testing the preset end to end for the first time, `e2-standard-4`
will get you there far faster for a few cents — and it is the difference
between finding out whether the *preset* works and finding out how slowly a
shared core unpacks scipy.

**Deleting the VM never deletes the bucket**, and this is structural rather
than a promise: `plexora/gcloud.py` has no storage-deletion verb for anything
to call.

### Credentials

Plexora never sees a Google password or token. Sign-in is `gcloud auth login`'s
own browser flow and the credential stays in gcloud's store; the saved profile
records which project, which bucket, which region and which machine type —
a description of a connection rather than a way into one, the same rule
`remotes.json` has always followed for SSH.

### Limits

- **`plexora connect <name>` from a terminal does not provision.** It connects
  to a VM that is already running. Creating one happens in the app, which is
  the surface that can show the ladder and the cost.
- **A gcsfuse mount is not a fast filesystem for small random writes.** Images
  read well; a project database on it will be slower than one on the boot
  disk. If that bites, point `--data-dir` at a local path and reach the bucket
  as a data node instead ([§7](#7-data-on-more-than-one-machine)).
- **Run end to end against a real account.** A VM created, the bucket mounted,
  Plexora installed on it and connected to. So the preset carries no
  "untested" badge — that badge means "we have not done this", and AWS is now
  the only preset it is still true of.

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
