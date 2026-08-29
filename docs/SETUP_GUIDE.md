# Setting up Plexora — a guide for everyone

This guide is written for people who want to look at their images, not for
people who want to learn about networking. It assumes you can install software
and open a terminal once, and nothing beyond that.

It is organised by **where things are**:

| If your situation is… | Go to |
|---|---|
| Plexora and all my data are on this computer | [Part 1 — Everything here](#part-1--everything-here) |
| My data is on a server, cluster, or another computer | [Part 2 — Everything over there](#part-2--everything-over-there) |
| Some data is here and some is over there | [Part 3 — Data in two places](#part-3--data-in-two-places) |

If you are not sure which, start with Part 1. It takes five minutes and it will
tell you whether you need the rest.

---

## The short version

Plexora tries very hard to require exactly one command:

```bash
plexora
```

Type that anywhere — your laptop, a cluster login node, a JupyterHub terminal,
inside an Open OnDemand session — and Plexora works out where it is running and
prints a link that actually works from where *you* are sitting. If it cannot
work something out, it says so in a sentence and tells you what to type
instead.

For a server you use regularly, you save it once and then connect by clicking a
button. You never memorise an SSH command, a port number, or a URL.

---

## Contents

- [Before you start](#before-you-start)
- [Part 1 — Everything here](#part-1--everything-here)
  - [1a. The Plexora app or terminal](#1a-the-plexora-app-or-terminal)
  - [1b. Jupyter on your own computer](#1b-jupyter-on-your-own-computer)
- [Part 2 — Everything over there](#part-2--everything-over-there)
  - [2a. Connect from Plexora on your own computer](#2a-connect-from-plexora-on-your-own-computer-recommended)
  - [2b. A terminal on the remote machine](#2b-a-terminal-on-the-remote-machine)
  - [2c. A cluster with a job scheduler](#2c-a-cluster-with-a-job-scheduler)
  - [2d. JupyterHub](#2d-jupyterhub)
  - [2e. Open OnDemand](#2e-open-ondemand)
  - [2f. Jupyter on a remote machine you SSH into](#2f-jupyter-on-a-remote-machine-you-ssh-into)
  - [2g. Google Colab](#2g-google-colab)
  - [2h. Cloud virtual machines, institutional proxies, containers](#2h-cloud-virtual-machines-institutional-proxies-containers)
- [Part 3 — Data in two places](#part-3--data-in-two-places)
  - [3a. Images on the cluster, table on your laptop](#3a-images-on-the-cluster-table-on-your-laptop)
  - [3b. Images on the cluster, viewer on your laptop](#3b-images-on-the-cluster-viewer-on-your-laptop)
  - [3c. A third machine](#3c-a-third-machine)
  - [Which machine does the work](#which-machine-does-the-work)
  - [One project, one database](#one-project-one-database)
- [Compatibility matrix](#compatibility-matrix)
- [Troubleshooting](#troubleshooting)
- [Screenshot index](#screenshot-index)

---

## Before you start

### Installing

Plexora is a Python package. If you have never used Python environments, this
is the whole ceremony, once:

```bash
conda create -n plexora python=3.13
conda activate plexora
pip install plexora
```

From then on, every session starts with:

```bash
conda activate plexora
```

You need this **on every machine that will run Plexora**. In Part 2 that means
the remote server as well as your laptop — see each scenario's "What needs to
be installed".

### The one thing worth knowing about SSH

Several scenarios below mention SSH. You do not need to understand it. You need
to know one thing: **if `ssh you@server` works in your terminal, everything in
this guide will work.** Plexora shells out to the same `ssh` you already use,
so your keys, your `~/.ssh/config`, your institution's two-factor setup and
your jump hosts all keep working without being described again.

If `ssh you@server` does *not* work yet, fix that first with your IT
department. Nothing here can substitute for it.

---

# Part 1 — Everything here

Plexora and your images are on the computer in front of you.

## 1a. The Plexora app or terminal

### When to use this setup

Your images and cell tables are on this computer's own disk (or on a drive
plugged into it, or a network drive it has mounted). This is the simplest case
and the one everything else is measured against.

### What needs to be installed

Plexora, on this computer. See [Installing](#installing).

### One-time setup

None. There is nothing to configure before the first run.

The first time it starts, Plexora tells you where it will keep your projects
and figures — usually a `Plexora` folder in your Documents. If you would rather
it lived somewhere with more space, change it under **Settings → Data**, or:

```bash
plexora config set data-dir /Volumes/BigDisk/plexora-data
```

> **[SCREENSHOT 1]** — *Settings → Data.* The current data directory, the rule
> that chose it, and the "Move to a different directory" field with its
> **Continue** button.

### How to launch Plexora

```bash
conda activate plexora
plexora
```

Your browser opens by itself. If it does not, the terminal prints a link like
`http://127.0.0.1:8000/` — click it.

### How to connect to the data

Use the **Import** page and point it at your files, or drag a folder in. This
is ordinary file-picking; nothing about it is network-related.

### What Plexora configures automatically

- The port. If 8000 is busy it quietly moves to a free one and tells you.
- Whether to open a browser. It will not try on a machine with no screen.
- Where your projects live, unless you have said otherwise.

### What you still need to provide

The location of your images. That is all.

### Subsequent sessions

```bash
conda activate plexora
plexora
```

Your projects are already there.

### Common problems

| What you see | What to do |
|---|---|
| `command not found: plexora` | You forgot `conda activate plexora`. |
| "Port 8000 is in use; using 8001 instead." | Nothing — that is Plexora being helpful. |
| The browser opens a blank page | Wait a few seconds and reload; a large image is being read. |
| You cannot find your old projects | Run `plexora where` — it prints the directory in use and *why* that one. |

---

## 1b. Jupyter on your own computer

### When to use this setup

You are already working in a notebook on this computer and want the viewer
inline, next to your analysis.

### What needs to be installed

```bash
pip install "plexora[jupyter]"
```

### One-time setup

None.

### How to launch Plexora

In a cell:

```python
import plexora

plexora.view("my_dataset")
```

Make that the last line of the cell. The viewer appears in the output.

### How to connect to the data

If the project already exists, name it as above. If it does not, you can build
one from file paths without leaving the notebook — see the "Registering data
from files you can see from the notebook" section of `DEPLOYMENT.md`.

### What Plexora configures automatically

It starts a small server beside your kernel, picks a free port, and shows it in
a frame. Because the notebook and the kernel are on the same computer, no
tunnelling or proxying is involved.

### What you still need to provide

Nothing.

### Subsequent sessions

The same cell. One server is reused across cells in the same notebook.

> **[SCREENSHOT 2]** — *A Jupyter notebook cell containing `plexora.view("tonsil")`
> with the viewer rendered inline underneath it.*

### Common problems

| What you see | What to do |
|---|---|
| A blank box where the viewer should be | Your kernel is not on this computer after all — go to [2f](#2f-jupyter-on-a-remote-machine-you-ssh-into). |
| `ModuleNotFoundError: plexora` | The notebook's kernel is a different environment from the one you installed into. |

---

# Part 2 — Everything over there

Plexora and all of your data are on a remote machine: a server, an HPC cluster,
or a cloud VM. Your computer only ever draws the picture.

**Which of the sections below is yours?**

- You want to click a button on your own computer → **[2a](#2a-connect-from-plexora-on-your-own-computer-recommended)**. This is the recommended
  route and the one this release was built for.
- You are already typing in a terminal on the remote machine → **[2b](#2b-a-terminal-on-the-remote-machine)**.
- The remote machine is a cluster where you are supposed to submit jobs → **[2c](#2c-a-cluster-with-a-job-scheduler)**.
- You reach it through a web page that says JupyterHub → **[2d](#2d-jupyterhub)**.
- You reach it through a web page that says Open OnDemand → **[2e](#2e-open-ondemand)**.

---

## 2a. Connect from Plexora on your own computer (recommended)

### When to use this setup

Any time your data is on a machine you can `ssh` into. This is the least
technical route: after a one-time setup you press **Connect** and wait.

**What this does, and what it does not.** Plexora keeps running on your own
computer. Connecting opens a *data node* on the remote machine — a small
process that reads files there and hands over only the part you are looking
at. Your projects, your figures and your database stay here. The files stay
there. Nothing is copied.

If you would rather Plexora *itself* ran on the remote machine, that is a
different setup and it is started over there rather than from here: see
[2b](#2b-a-terminal-on-the-remote-machine), [2c](#2c-a-cluster-with-a-job-scheduler)
or `plexora connect` at the end of this section.

### What needs to be installed

- Plexora on **your own computer**.
- Plexora on **the remote machine**. Ask whoever administers it, or install it
  into your own environment there exactly as in [Installing](#installing).

### One-time setup

1. Start Plexora on your own computer (`plexora`).
2. Go to **Settings → Remote servers**.
3. If you work somewhere Plexora already knows about, press **Start from a
   preset…** and pick it — an HMS O2 or a generic Slurm cluster comes with the
   partition, walltime and memory already filled in, and asks you only for
   your username. Otherwise fill in two fields and press **Save server**:

   | Field | What to put | Example |
   |---|---|---|
   | **Name** | Anything you like. It is just a label. | `hpc` |
   | **Address** | Exactly what you type after `ssh` | `jane@login.cluster.edu` |

That is the minimum, and for many servers it is all you need.

> Some presets are marked **untested** on their card. They are shaped from a
> site's published documentation rather than from a session we have run, so
> treat the values as a starting point.

> **[SCREENSHOT 3]** — *Settings → Remote servers, empty state.* The "Add a
> server" card showing the Name, Address and "Open project" fields, the
> collapsed **Advanced** section, and the **Save server** button.

**If the connection later fails saying it could not run `plexora`**, open
**Advanced** and set *How to start Plexora over there*. This is by far the most
common thing that needs adjusting, because a program on a server is often only
on your PATH after you load a module or activate an environment.

The environment's own path is enough, and it is the thing you can actually look
up — `conda env list` on the server prints it:

```
/home/jane/miniconda3/envs/imaging
```

Plexora fills in `bin/plexora` itself. A full command works too, if reaching
Plexora on that server is genuinely a shell expression:

```
conda run --no-capture-output -n imaging plexora
module load python && plexora
```

> **[SCREENSHOT 4]** — *The Advanced section expanded*, showing "How to start
> Plexora over there", the "This is a cluster login node" checkbox, "Job
> options", and the data-node fields.

### How to connect

Press **Connect** on the saved server. A dialog opens and shows the whole of
it: the steps it is working through (*Reaching the machine* → *Signing in* →
*Waiting for the scheduler*, on a cluster → *Opening the tunnel* → *Starting
the data node*), the sentence saying what it is doing now, and the connection
log underneath — SSH's own output, and the remote machine's, as it arrives.

Closing that dialog does **not** cancel the connection. On a cluster, waiting
for the scheduler is a genuine fifteen minutes; **Continue in background**
leaves it running and it goes on showing up in Settings and behind the globe
in the toolbar. **Stop connecting** is the button that ends it.

> **[SCREENSHOT 5]** — *The connection dialog mid-connection*, showing the step
> list with one step active, the phase sentence, and the terminal log.

> **[SCREENSHOT 6]** — *A connected server in Settings*, showing the green
> "Connected" badge, the node it registered, and **Disconnect**.

### If it asks you for a password

Some servers use passwords, or a code from an app, or ask you to confirm a new
host key. When that happens the question appears **on this page**, worded
exactly as SSH asked it, with a box to type the answer into. Type it and press
**Send**.

> **[SCREENSHOT 7]** — *A server in the "Needs your password" state*, showing
> the amber prompt box containing SSH's own prompt text and a masked input with
> a **Send** button.

**Plexora never stores your password.** There is no field for one in the saved
profile and no file it could be written to. What you type is handed straight to
SSH and forgotten.

> **A note for Windows users.** This in-page prompt relies on a feature of
> OpenSSH that is reliable on macOS and Linux and unreliable on Windows. On
> Windows, prefer an SSH key or an agent (which is better practice anyway); if
> you must use a password, use `plexora connect` in a terminal instead, where
> SSH can prompt you directly.

### How to use the data

Every field that asks for a file has a small **L | R** switch beside it —
**L**ocal or **R**emote. Flip it to **R** and the field asks which machine;
pick the server you just connected. **Browse…** then lists that machine's
filesystem, and the file stays where it is.

You can mix them in one project: an image on the cluster and a table on your
laptop is two choices on two fields, not a mode. The globe in the toolbar says
which machines are connected, whether they are answering, and which one the
image on screen is being read from.

### What Plexora configures automatically

- The SSH connection, using your existing keys, config and jump hosts.
- A free port on each end, and the tunnel between them.
- Starting the data node over there, and shutting it down when you disconnect.
- Registering that node, so every data field can offer it by name.
- Retrying on a different port if the one it picked was taken.

### What you still need to provide

- The address (`user@host`) — nothing can guess this.
- Your password or key, when the server asks.
- The launch command, if `plexora` is not on the remote PATH.
- Whether it is a cluster login node (see [2c](#2c-a-cluster-with-a-job-scheduler)).

### Subsequent sessions

Start Plexora, **Settings → Remote servers**, **Connect**. Two clicks.

### Moving Plexora itself there instead

Sometimes you want the whole of Plexora running on the remote machine — the
processing is heavy, or the files are large enough that even reading a tile
over the wire is slow. That is a launch decision made *at* that machine rather
than a setting inside this one, and `plexora connect` is the command that does
it from here: it starts Plexora over there and points this browser at it
through the tunnel. From that Plexora, the cluster is "Local".

It reads the same list of saved servers:

```bash
# First time — connect and remember it:
plexora connect jane@login.cluster.edu --save hpc

# Every time after that:
plexora connect hpc

# The saved server, but a different project today:
plexora connect hpc other-study
```

### Common problems

| What you see | What to do |
|---|---|
| "The remote host could not run 'plexora'…" | Set *How to start Plexora over there* — see the one-time setup above. |
| "The remote host rejected the login." | Check the username in the Address field, and that `ssh user@host` works in a terminal. |
| "SSH refused to continue because this host's key…" | Do not click past this. Ask your administrator whether the server was rebuilt. |
| "3 connections are already being opened." | Something is retrying. Disconnect the stuck one. |
| It says Connected but a file will not load | Open the globe in the toolbar. If the machine says *Not answering*, the tunnel has died — disconnect and connect again. |

---

## 2b. A terminal on the remote machine

### When to use this setup

You are already SSH'd into the machine and would rather stay there. Also the
fallback whenever [2a](#2a-connect-from-plexora-on-your-own-computer-recommended)
cannot be made to work.

### What needs to be installed

Plexora on the remote machine.

### One-time setup

None.

### How to launch Plexora

```bash
plexora
```

Plexora notices it is being run over SSH and prints the exact command to paste
into a **second terminal on your own computer**, plus the address to open
afterwards. Something like:

```
Detected a machine reached over SSH; configuring the URL to match (--no-detect turns this off).

[plexora-remote] node=server.example.edu port=8000

Plexora is running on server.example.edu, bound to 127.0.0.1:8000.
From your own machine, run:
  ssh -N -L 8000:127.0.0.1:8000 jane@server.example.edu
then open  http://localhost:8000/
```

Leave both running.

> **[SCREENSHOT 8]** — *A terminal showing the above output*, with the tunnel
> command highlighted.

### How to connect to the data

Import projects normally in the browser tab; the paths you type are paths on
the **remote** machine.

### What Plexora configures automatically

Everything except the second terminal: it detects that it is on a remote
machine, keeps the port private to that machine, and writes the tunnel command
out with the real hostname, your username and the real port already filled in.

### What you still need to provide

Running that one `ssh` command on your own computer.

### Subsequent sessions

The same two commands — or switch to [2a](#2a-connect-from-plexora-on-your-own-computer-recommended),
which does both for you.

### Common problems

| What you see | What to do |
|---|---|
| A plain `http://127.0.0.1:8000/` and no tunnel instructions | Detection did not fire. Add `--remote`. |
| The tunnel command fails with "address already in use" | Another tunnel is using that port on your computer. Change the first number: `-L 8010:127.0.0.1:8000`, then open `http://localhost:8010/`. |

---

## 2c. A cluster with a job scheduler

### When to use this setup

Your institution's cluster has *login nodes* (where you land when you SSH) and
*compute nodes* (where real work is supposed to happen), and you have been told
not to run heavy things on the login node. Reading a large image is a heavy
thing.

### What needs to be installed

Plexora on the cluster.

### One-time setup

In **Settings → Remote servers**, save the server as in
[2a](#2a-connect-from-plexora-on-your-own-computer-recommended), then open
**Advanced** and:

1. Tick **This is a cluster login node — run Plexora inside a job**.
2. In **Job options**, put whatever your site wants, e.g.
   `-p interactive -t 4:00:00 --mem 32G`. Leave it empty to accept the
   defaults.

### How to launch Plexora

Press **Connect**. The status will sit at **Queued** — *"Waiting for the
scheduler to allocate a node. This can take a while on a busy queue."*

**That is not a problem and not a hang.** You have asked a shared machine for
resources and you are in a queue. Plexora waits up to 15 minutes by default. It
then finds the compute node the scheduler gave you, builds the tunnel to it,
and connects.

> **[SCREENSHOT 9]** — *A saved cluster server in the "Queued" state*, showing
> the scheduler wait sentence.

### How to connect to the data

Normally, on the remote tab. Point projects at your scratch space.

### What Plexora configures automatically

- Submitting the job.
- Discovering which compute node the scheduler picked — which cannot be known
  in advance and is the reason this is harder than [2a](#2a-connect-from-plexora-on-your-own-computer-recommended).
- A two-hop tunnel from your computer, through the login node, to that compute
  node.
- Ending the job when you disconnect.

### What you still need to provide

- The job options your site expects.
- Patience while the queue does its thing.
- Occasionally: tick **Forward from the login node** as well, if your cluster
  refuses SSH connections into compute nodes. If the connection dies with
  something about not being able to reach the compute node, try this.

### Subsequent sessions

**Connect**. A new job is submitted each time, because the last one ended when
you disconnected.

### Common problems

| What you see | What to do |
|---|---|
| Stuck on "Queued" for a very long time | Your job request is too large or the queue is busy. Ask for less time or less memory. |
| Connects, then dies immediately | The job started and was killed — usually a memory limit. Raise `--mem`. |
| "…could not reach the compute node" | Tick **Forward from the login node**. |

### The same thing from a terminal

```bash
plexora connect jane@login.cluster.edu \
    --srun "-p interactive -t 4:00:00 --mem 32G" \
    --save hpc
```

---

## 2d. JupyterHub

### When to use this setup

You log into a web page, get a Jupyter environment, and your data is on that
machine. Common at institutions with a shared analysis platform.

### What needs to be installed

- Plexora, in the environment your notebooks run in.
- `jupyter-server-proxy`, in the environment running the **Jupyter server**.
  That is often not the same environment, and often not one you control — if
  the link below 404s, this is what is missing and your administrator has to
  install it.

### One-time setup

None.

### How to launch Plexora

**Either** in a notebook cell:

```python
import plexora
plexora.view("my_dataset")
```

**Or** in a JupyterHub terminal (File → New → Terminal):

```bash
plexora
```

The terminal route prints something like:

```
Detected a Jupyter server that can proxy this port; configuring the URL to match.

This kernel is not on the machine holding your screen, so Plexora is reached
through your Jupyter server rather than at localhost.

Open this in the browser tab your notebook is already in:
  /user/jane/proxy/8000/

That is a path, not a whole address: put it after the host your notebook is
already open at, e.g.
  https://jupyter.your-institution.edu/user/jane/proxy/8000/
```

Copy that path onto the end of the address already in your browser's address
bar.

> **[SCREENSHOT 10]** — *A JupyterHub terminal showing the proxy path output*,
> with a browser address bar above it showing the assembled URL.

### How to connect to the data

Normally. The files are on the same machine as the kernel.

### What Plexora configures automatically

- That it is inside a hub at all.
- Your notebook's own URL prefix.
- The port, and writing that port into the path.
- A check that the Jupyter server really will proxy that port — if it will not,
  Plexora says so instead of handing you a URL that 404s.

### What you still need to provide

The first half of the address (your hub's hostname). Plexora deliberately does
not guess it: nothing on that machine records the public address you reached it
at, and a wrong guess produces a broken link nobody could debug. It is already
in your address bar.

### Subsequent sessions

The same. The port may differ, so re-read the printed path.

### Common problems

| What you see | What to do |
|---|---|
| "Note: your Jupyter server does not proxy arbitrary ports…" | `jupyter-server-proxy` is missing in the *server's* environment. Ask your administrator. |
| A 404 page at the proxy URL | Same cause. |
| It printed a `127.0.0.1` URL instead | Detection did not fire. Run `plexora --base-url /user/YOURNAME/`. |

---

## 2e. Open OnDemand

### When to use this setup

Your institution gives you a web portal with buttons like "Jupyter Notebook" or
"Desktop", and sessions run inside cluster jobs.

### What needs to be installed

Plexora, in the environment your OnDemand session uses. Nothing else —
notably **not** `jupyter-server-proxy`; the portal does the proxying itself.

### One-time setup

None.

### How to launch Plexora

In a terminal inside your OnDemand session:

```bash
plexora
```

It prints a complete, token-protected link:

```
Detected an Open OnDemand session; configuring the URL to match.

Plexora is running on c42.cluster.edu, bound to 0.0.0.0:8000 so Open OnDemand
can proxy it.
Open this in the browser your OnDemand session is already in:
  https://<your-OnDemand-host>/rnode/c42.cluster.edu/8000/?token=AbC123…

Replace <your-OnDemand-host> with the host the OnDemand portal itself is open at.
```

> **[SCREENSHOT 11]** — *An OnDemand terminal showing the `/rnode/` link with
> its token.*

### How to connect to the data

Normally.

### What Plexora configures automatically

- That it is inside OnDemand, from the shape of the session's own URL.
- The compute node's name, spelled the way the portal itself routes it.
- Binding an address the portal can actually reach.
- A one-time token, because that port is briefly visible to other accounts on
  the cluster.

### What you still need to provide

The portal's hostname — again, it is in your address bar.

**Treat the printed link like a password.** The token in it is what keeps other
people on the cluster out.

### Subsequent sessions

The same. The node and port change every session, so use the new link.

### Common problems

| What you see | What to do |
|---|---|
| The link gives "This viewer requires a token" | You dropped the `?token=…` part. Use the whole link. |
| The link times out | Your site may not have the `/rnode/` door enabled. Ask your administrator, or fall back to [2b](#2b-a-terminal-on-the-remote-machine). |

---

## 2f. Jupyter on a remote machine you SSH into

### When to use this setup

You started `jupyter lab` yourself on a workstation or server after SSH-ing in.

### What needs to be installed

Plexora, and `jupyter-server-proxy` in the same environment as your Jupyter.

### One-time setup

None.

### How to launch Plexora

`plexora.view("my_dataset")` in a cell. Plexora sees that the kernel is not on
the machine holding your screen and produces a proxied URL rather than a
localhost one that would point at your own laptop.

If you already forward the Jupyter port yourself (VS Code Remote does this),
Plexora detects that too and leaves it alone.

### What you still need to provide

Nothing, usually. If the guess is wrong:

```python
plexora.view("my_dataset", proxy=False)   # I forward ports myself
plexora.view("my_dataset", base_url="/")  # my Jupyter is at the root
```

### Common problems

| What you see | What to do |
|---|---|
| A blank frame | The proxy is missing. Install `jupyter-server-proxy` and restart Jupyter. |
| It proxied when you did not want it to | `proxy=False`. |

---

## 2g. Google Colab

### When to use this setup

Your notebook is on Colab and your data has been uploaded or mounted there.

### What needs to be installed

```python
!pip install plexora
```

### How to launch Plexora

**In a cell — this is the only way that works on Colab:**

```python
import plexora
plexora.view("my_dataset")
```

Running `plexora` in a Colab *shell* cannot work, and Plexora will tell you so
rather than print a broken link: Colab's proxy address is only knowable by
asking the notebook front-end in JavaScript, and a shell has no front-end to
ask.

### What you still need to provide

Your data, in the Colab session. Colab is a good place to try Plexora and a
poor place to keep large images.

### Common problems

| What you see | What to do |
|---|---|
| "This looks like Google Colab…" printed by a shell command | Use `plexora.view()` in a cell instead. |
| The viewer appears then disappears | Colab recycled the runtime. Re-run the cell. |

---

## 2h. Cloud virtual machines, institutional proxies, containers

### Cloud VMs (AWS, GCP, Azure)

A cloud VM is an SSH-accessible server. Use
[2a](#2a-connect-from-plexora-on-your-own-computer-recommended) — save it as a
server and press Connect. Do **not** open the port to the internet; the tunnel
means you do not have to.

### Institutional servers behind a reverse proxy

If your IT department serves Plexora at something like
`https://tools.institution.edu/plexora/`, they must:

- strip the `/plexora` prefix before forwarding (Plexora always serves at the
  root), and
- start it with `PLEXORA_BASE_URL=/plexora` so the links it generates carry the
  prefix.

This is one line each in the proxy config and the service file. See the
Reference section of `DEPLOYMENT.md`.

### Containers

There is a `Dockerfile`. The published image sets `PLEXORA_HOST=0.0.0.0`
because a published port must be reachable from outside the container. Mount
your data and your project directory as volumes:

```bash
docker run -p 8000:8000 \
  -v /path/to/images:/data \
  -v /path/to/plexora-data:/root/plexora-data \
  plexora
```

Automatic environment detection is deliberately **off** inside a container:
`PLEXORA_HOST` being set is taken as a decision that has already been made.

---

# Part 3 — Data in two places

Sometimes one project's data is not all in one place. The usual shape is:

- **the images** are enormous and live on the cluster, and
- **the cell table** is a few hundred megabytes and lives on your laptop, where
  you have been analysing it.

Plexora handles this with **data nodes**. A data node is a small server that
does nothing but hand out bytes from files it has been pointed at. It has no
viewer, no project list and no database of its own — it is a doorway to a
directory.

You do not have to understand any of that to use it. You list which files are
where, and Plexora starts, connects and registers the nodes itself.

## Which machine does the work

**The rule: the viewer runs next to the images.** Everything else follows from
it.

Images are read tile by tile, thousands of times, as you pan and zoom. A cell
table is read a handful of times. So the machine holding the images should do
the heavy reading, and the small files should travel — not the other way round.

| Where the images are | Where the viewer should run | Which section |
|---|---|---|
| On the cluster | On the cluster | [3a](#3a-images-on-the-cluster-table-on-your-laptop) |
| On the cluster, but they are modest | On your laptop | [3b](#3b-images-on-the-cluster-viewer-on-your-laptop) |
| On your laptop | On your laptop | [3c](#3c-a-third-machine) |

If you pick wrong, nothing breaks — it is just slower.

## One project, one database

A reasonable worry: if my data is on three machines, do I end up with three
project lists, three sets of figures, and three places to look?

**No.** There is exactly one project database, and it lives with **the viewer**
— the machine you actually open in your browser. It records "this project's
image is resource `tonsil` on node `hpc`" as a pointer. Data nodes store
nothing: they are stateless, hold no project list, and can be shut down and
restarted on a different port without your project noticing.

The practical consequences:

- Your figures, channel settings, gates and project names are in **one** place.
- Moving a data node, or reconnecting it on a new port, does not affect any of
  them.
- If you switch which machine runs the viewer, you switch project databases —
  so pick one and stay there.
- If a node is unreachable when a project opens, the project **still opens**,
  without that layer, and a banner at the top of the page says which node and
  why.

> **[SCREENSHOT 12]** — *The viewer with an amber banner across the top*
> reading "The cell table for this project could not be loaded from data node
> 'hpc-scratch': …" with an **Open Settings** link and a dismiss ×.

---

## 3a. Images on the cluster, table on your laptop

The most common split. The viewer runs on the cluster beside the images; your
laptop shares the cell table back to it over the same connection.

### When to use this setup

Your images are too big to copy and your AnnData/CSV has never left your
laptop.

### What needs to be installed

Plexora on both machines.

### One-time setup

In **Settings → Remote servers**, save the cluster as usual. That is all — you
do **not** list the files to share in advance, and there is no box to do it in.
Which machine each file is on is chosen when you add the data.

### How to launch Plexora

This arrangement runs the viewer on the *cluster*, so it is launched from a
terminal on your own computer rather than from the Settings page:

```bash
plexora connect hpc
```

That will, in order: start a data node on your laptop, open the SSH connection
with a reverse channel so the cluster can reach back to it, start Plexora over
there, register your laptop's node with it, and point your browser at it.

> **[SCREENSHOT 13]** — *The terminal output of `plexora connect hpc`*, showing
> the local data node starting, the tunnel, and the URL it hands you.

### How to connect to the data

In the Plexora that opens — the one running on the cluster — import a project. Every data field has a compact
**L | R** switch in the row, immediately before the path box — **L** for this
computer, **R** for another machine. (Hover it, or read it with a screen
reader, and it says so; the chip beside it names the machine **R** currently
means.)

- **Image** → **R**, then browse the cluster's filesystem.
- **Cell table** → **L** (the default), then browse your laptop. Plexora hands
  the path to the node it just started here.

With one other machine connected, pressing **R** simply takes it — there is
nothing to pick from a list of one. With none connected, **R** opens the
connection dialog instead. The chip always opens the full list, so a machine
adopted that way can still be changed.

Nothing had to be declared first, and the same switch is on the Edit page and
in the "this tool needs a mask" dialog, so a source can be changed later — the
primary image excepted, which is fixed once the project exists because every
ROI, figure and coordinate lives in its pixel space.

### What Plexora configures automatically

Starting the local node, its port, its access token, the reverse channel, and
the registration — including the fact that your *browser* should read that node
directly (it is on the same laptop) while the *viewer* reads it back down the
SSH connection. Those are two different addresses for one node and getting them
right by hand is fiddly.

### What you still need to provide

Which file goes in which field, and which side of the switch it is on.

### Subsequent sessions

**Connect**. The node gets a new port and a new token each time and Plexora
updates the registration itself; your project keeps working without being
touched.

### From a terminal

```bash
plexora connect hpc --local-serve table:cells=~/study/cells.h5ad
```

---

## 3b. Images on the cluster, viewer on your laptop

The mirror image: everything stays on your laptop except the pixels.

### When to use this setup

Your images are large but not enormous, you want your projects and figures kept
locally, or the cluster cannot run a viewer for you.

### What needs to be installed

Plexora on both machines.

### One-time setup

Save the cluster in **Settings → Remote servers**, exactly as in
[2a](#2a-connect-from-plexora-on-your-own-computer-recommended). Nothing else —
in particular, no list of files.

### How to launch Plexora

Just `plexora`. This is your ordinary local Plexora, and it stays that way.

### How to connect to the data

Import a project as usual. On the **image** field, press **Remote**. A dialog
lists your saved servers; choose the cluster and press **Connect**. Plexora
opens the SSH connection and starts a data node over there — asking for your
password in the dialog if it needs one — and then hands you back to the form,
where **Browse** now lists the *cluster's* filesystem. Pick the image and carry
on.

The cell table and the mask can each go the other way, or the same way, or a
different server again. The switch is per field.

> **[SCREENSHOT 14]** — *The place picker over a half-filled import form*,
> showing a saved server with a **Connect** button and another already marked
> **Connected**.

### What Plexora configures automatically

The SSH connection, the port on each end, the node's token, the registration
into this machine's node list, the browser's CORS origin so tiles are read
directly rather than proxied, and shutting the node down when you disconnect.

### What you still need to provide

Which file goes in which field.

### Subsequent sessions

Open the project. If the connection is not up, the banner says so; reconnect it
from **Settings → Remote servers** or by pressing **Remote** on any field and
choosing that server again. The project itself needs no changes — the node comes
back under the same name, serving the same files under the same ids.

### The same thing from a terminal

```bash
# Bring the remote images here, and keep it open
plexora node connect jane@login.cluster.edu \
    --serve image:tonsil=/scratch/jane/tonsil.ome.tif --name hpc
```

Still supported, and still the right answer for a script. The difference is
that it needs the paths up front, which is the thing the switch removed.

### Common problems

| What you see | What to do |
|---|---|
| "The data node did not answer … within 60s" | A segmentation mask is being converted. Raise `--timeout`, or run `plexora node prepare` on it once over there. |
| Tiles load slowly | Expected — every tile crosses the network. Consider [3a](#3a-images-on-the-cluster-table-on-your-laptop) instead. |

---

## 3c. A third machine

Nothing about the above is limited to two machines. A project can read its
image from one node, its mask from another and its table from a third; nodes
are registered independently under **Settings → Data nodes** and each project
resource picks one by name.

Use `plexora node connect` (as in [3b](#3b-images-on-the-cluster-viewer-on-your-laptop))
once per machine, or `plexora node serve` directly on a machine you can reach
without a tunnel:

```bash
# On the storage server:
plexora node serve --serve image:archive=/mnt/archive/slide.ome.tif
```

It prints a registration line to copy into **Settings → Data nodes** on the
viewer's machine.

> **[SCREENSHOT 15]** — *Settings → Data nodes with three nodes listed*, one
> reachable, one marked as automatically managed by a saved server, and one not
> answering.

### One thing that still has to be typed by hand

If your browser reaches a node through an Open OnDemand portal, the node's
**Browser address** field has to be filled in manually
(`/rnode/compute-3/8642/`). Nothing on either machine records the portal's
address, so it cannot be worked out. Every other combination is automatic.

---

# Compatibility matrix

**Legend:** ● recommended · ○ works · – not applicable

| Interface | 1. All local | 2. All remote | 3. Split data |
|---|:--:|:--:|:--:|
| Plexora app / local terminal | ● | – | ● (as the viewer, [3b](#3b-images-on-the-cluster-viewer-on-your-laptop)) |
| **Settings → Remote servers** (local app → remote) | – | ● | ● ([3a](#3a-images-on-the-cluster-table-on-your-laptop)) |
| `plexora connect` (local terminal → remote) | – | ● | ● ([3a](#3a-images-on-the-cluster-table-on-your-laptop)) |
| `plexora node connect` (bring remote data here) | – | ○ | ● ([3b](#3b-images-on-the-cluster-viewer-on-your-laptop)) |
| Remote terminal (`plexora` over SSH) | – | ● | ○ |
| HPC login node + scheduler (`--srun`) | – | ● | ○ |
| Local Jupyter / JupyterLab | ● | – | ○ |
| Remote Jupyter (you started it) | – | ● | ○ |
| JupyterHub | – | ● | ○ |
| Open OnDemand | – | ● | ○ (browser address typed by hand) |
| Google Colab | ○ | ○ | – |
| Cloud VM | – | ● | ○ |
| Reverse-proxied institutional server | ○ | ● | ○ |
| Docker container | ● | ○ | ○ |

Notes on the "–" entries:

- The local app cannot be the *whole* answer for remote data; it becomes the
  front end for one of the connect routes instead.
- Colab has no persistent storage worth splitting data across, and no SSH.
- JupyterHub and OnDemand are, by construction, "the data is where the kernel
  is" environments; splitting across them works but the portal address problem
  above applies.

---

# Troubleshooting

### "I do not know which URL to open"

Whatever Plexora printed last. Every route above ends with Plexora telling you
the address; none of them expects you to construct one.

### "It worked yesterday and today the link is dead"

Ports and compute nodes change between sessions. Reconnect and use the new
link. This is exactly what saved servers exist to make painless.

### "A layer of my project is missing"

Look at the top of the page for an amber banner naming the node. Reconnect it
under **Settings → Remote servers** and reload the project.

### "The connection log is full of things I do not understand"

That is SSH talking. The line that matters is almost always the last one before
it stopped. The three that come up most:

| In the log | Meaning |
|---|---|
| `command not found` | Set *How to start Plexora over there*. |
| `Permission denied` | Wrong username, or the key/password was rejected. |
| `Host key verification failed` | The server's identity changed. Ask before proceeding. |

### "How do I turn the automatic detection off?"

```bash
plexora --no-detect
```

Every flag it would have set is still available to set yourself: `--remote`,
`--ood`, `--base-url`, `--host`.

### "Where did my projects go?"

```bash
plexora where
```

It prints the directory in use and the rule that chose it.

### Getting more detail

`DEPLOYMENT.md` in this repository is the same material for a technical reader,
with every flag, every environment variable and the reasoning behind the
choices.

---

# Screenshot index

For whoever is capturing these. Each should be taken at a browser width of
about 1400px, in the default dark theme, with a realistic project name rather
than "test".

| # | Where | Shows |
|---|---|---|
| 1 | Settings → Data | Current directory, rule, change field |
| 2 | Jupyter notebook | `plexora.view()` with inline viewer |
| 3 | Settings → Remote servers | Empty state and the Add-a-server form |
| 4 | Settings → Remote servers | Advanced section expanded |
| 5 | Settings → Remote servers | A server mid-connection |
| 6 | Settings → Remote servers | A connected server with Open button |
| 7 | Settings → Remote servers | The password prompt state |
| 8 | Terminal | `plexora` over SSH printing tunnel instructions |
| 9 | Settings → Remote servers | The "Queued" scheduler state |
| 10 | JupyterHub terminal + address bar | The proxy path being assembled |
| 11 | OnDemand terminal | The `/rnode/` link with token |
| 12 | Viewer | The unavailable-resource banner |
| 13 | Settings → Remote servers | Connected, with a data node line |
| 14 | Settings → Data nodes | One reachable node and its resources |
| 15 | Settings → Data nodes | Three nodes in three states |
