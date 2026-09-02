"""Starting points for "add a server", for the sites people actually use.

Adding a remote server means answering questions about somebody else's
cluster: does it run a scheduler, which partition, does ssh into a compute
node work, is `plexora` on a non-interactive PATH. Those answers are properties
of the SITE and are the same for everybody who works there -- so asking each
person to discover them, once, by failing, is the wrong shape.

A recipe answers them in advance and asks only for what genuinely differs: the
username, and sometimes how long the job should last. It composes a `Remote`,
which is a plain saved profile with **nowhere to put a credential** -- the
no-secret invariant holds here by construction rather than by care.

**A recipe is a starting point, never a lock.** What it produces is an ordinary
profile the user can edit afterwards in Settings, and a site that changes its
partition names does not break anybody: it makes one preset slightly wrong,
visibly, on the form where it is being filled in.

**Untested presets say so.** A preset that names a particular institution's
cluster asserts facts about somebody else's machine, and can be wrong about
them. HMS O2, MGB ERISTwo and Google Cloud are pinned to observed behaviour
(see DEPLOYMENT.md, which quotes a real O2 session); what is left is shaped
from published documentation and carries `site=True, tested=False`, which the
form renders as a badge. Presenting a guess with the same confidence as a
verified fact is how somebody spends an afternoon on a partition name that
never existed.

The generic shapes -- "a Slurm cluster", "a plain SSH server" -- assert nothing
about any particular machine, because the user supplies the address. They carry
no badge, and there is nothing about them to test.

**Nothing here wraps a job in anything but `srun`.** Plexora's scheduler
support IS `srun` (connect.srun_command_line), so an LSF or PBS site gets the
plain-SSH shape and a note telling them to get their own interactive session
first. Offering an `bsub` box that quietly did nothing would be worse than not
offering one.

**One preset asks for something else, and says so in its `extra`.** Every
recipe above describes a machine somebody already has; the Google Cloud one
describes a machine that does not exist yet, and its questions are therefore
not in the vocabulary above -- which project, which bucket, how big a VM.
`extra["flow"]` is the marker for that: it tells the form to draw its own
boxes instead of the standard ones, and it tells `compose` to take a different
branch. Everything after that is the same as every other preset -- one saved
profile, through one save, with nowhere in it to put a credential. Google's
own credential store keeps the way in; this file records only the description.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from plexora import gcloud
from plexora.server.models.remotes import WORKSTATION_OS


#: What a recipe may ask for beyond the username, keyed by the field it fills.
#: Kept small on purpose: every extra box is a question somebody has to have an
#: opinion about before they can press Connect, and the whole point is that the
#: site's defaults are usually right.
ASK_USER = "user"
ASK_WALLTIME = "walltime"
ASK_CORES = "cores"
ASK_MEMORY = "memory"
#: Which operating system is on the far side. Part of this vocabulary rather
#: than a `flow` of its own, which is the whole design of the workstation
#: preset: it wants the standard username and host boxes plus one more
#: question, and a flow means "draw nothing standard at all" -- an empty
#: `target_template` and an empty `ask`, which is the opposite of what this is.
ASK_OS = "os"

#: What a job asks the scheduler for when nobody has said otherwise. These are
#: the numbers a multiplexed image actually needs -- a 40-channel pyramid is
#: tens of gigabytes before anything is drawn -- so a default that merely lets
#: the process start is a default that gets killed halfway through an import.
#: They are constants rather than three literals because they appear in three
#: places that must agree: the srun line a preset composes, the boxes the form
#: fills in, and the placeholder on the Settings page. A default nobody can see
#: is a default nobody can correct, so the form shows them rather than leaving
#: empty boxes over an invisible site value.
DEFAULT_WALLTIME = "4:00:00"
DEFAULT_CORES = "16"
DEFAULT_MEMORY = "128G"
DEFAULT_PARTITION = "interactive"

#: The whole line, assembled once. Flag order matters only in that splicing
#: preserves it -- see `_with_flag`.
DEFAULT_SRUN = (f"-p {DEFAULT_PARTITION} -t {DEFAULT_WALLTIME} "
                f"-c {DEFAULT_CORES} --mem {DEFAULT_MEMORY}")

#: The same line with the three managed flags taken out -- what belongs in the
#: "additional scheduler arguments" box when the three above have boxes of
#: their own. Derived rather than written out, so it cannot fall out of step
#: with DEFAULT_SRUN.
DEFAULT_SRUN_EXTRA = f"-p {DEFAULT_PARTITION}"

#: The flags the form has a box for, in the order they are spliced. Everything
#: else in a site's line -- the partition, an account, a QoS, a `--gres` -- is
#: what the Advanced box is for. The split is here rather than in the browser
#: so that the line somebody edits and the line the server composes cannot
#: disagree about which flags belong to whom: a walltime box reading 4:00:00
#: above an Advanced line reading `-t 8:00:00` is two answers to one question.
MANAGED_FLAGS = (("-t", "walltime"), ("-c", "cores"), ("--mem", "memory"))

#: The one preset that asks its own questions -- see the module docstring.
#: Named rather than spelled out at each of its three uses, because the string
#: is a contract between this file, `compose`, and the browser's form.
FLOW_GCLOUD = "gcloud"


def defaults() -> dict:
    """What the form should show in the walltime, cores and memory boxes."""
    return {
        "walltime": DEFAULT_WALLTIME,
        "cores": DEFAULT_CORES,
        "memory": DEFAULT_MEMORY,
        "srun": DEFAULT_SRUN,
        "srun_extra": DEFAULT_SRUN_EXTRA,
    }


def split_srun(srun) -> dict:
    """One stored `srun` line, as the boxes a form shows it in.

    The inverse of `join_srun`, and the reason the Settings form can offer
    Cores / Memory / Time as three fields over a store that holds one string.
    `None` -- no scheduler at all -- comes back as empty boxes rather than as
    nothing, because the form has to render something either way and an
    unticked switch is what carries the distinction.
    """
    parts = (srun or "").split()
    out = {"walltime": "", "cores": "", "memory": "", "extra": ""}
    for flag, key in MANAGED_FLAGS:
        for index, part in enumerate(parts):
            if part == flag and index + 1 < len(parts):
                out[key] = parts[index + 1]
                break
    out["extra"] = _without_flags(srun, [flag for flag, _ in MANAGED_FLAGS])
    return out


#: What Slurm's `-t` calls "no limit". Either spelling means the job is not on
#: a clock, which is a real answer and not the same as an unparseable one.
UNLIMITED = ("unlimited", "infinite", "0", "0:00", "0:00:00")


def walltime_seconds(text) -> int | None:
    """A Slurm `-t` value as a number of seconds, or None.

    None means "no clock to show": no walltime given, a limit of none, or a
    spelling this does not understand. All three come out the same way on
    purpose -- what a countdown must never do is invent a deadline, because a
    wrong one is worse than no clock at all. Somebody who is told they have
    twenty minutes left when the job is not on a clock will save and reconnect
    for nothing; somebody told nothing simply carries on.

    Slurm accepts six shapes and they are genuinely ambiguous without the rule:
    a bare number is MINUTES, `x:y` is minutes:seconds, and it is the day
    separator that makes the colon groups mean hours. So `-t 30` is half an
    hour, `-t 30:00` is also half an hour, and `-t 1-0` is a day.

        minutes | minutes:seconds | hours:minutes:seconds
        days-hours | days-hours:minutes | days-hours:minutes:seconds
    """
    raw = str(text or "").strip()
    if not raw or raw.lower() in UNLIMITED:
        return None
    days = 0
    if "-" in raw:
        head, _, raw = raw.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
        if not raw:
            raw = "0"
        # Past the separator the groups are hours-first, always: `1-2` is a day
        # and two HOURS, where a bare `2` would have been two minutes.
        parts = raw.split(":")
        if len(parts) > 3:
            return None
        parts += ["0"] * (3 - len(parts))
    else:
        parts = raw.split(":")
        if len(parts) == 1:
            parts = ["0", parts[0], "0"]      # minutes
        elif len(parts) == 2:
            parts = ["0"] + parts             # minutes:seconds
        elif len(parts) != 3:
            return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def srun_seconds(srun) -> int | None:
    """The time limit a stored `srun` line asks for, in seconds, or None.

    The two halves that already exist, joined: `split_srun` finds the flag and
    `walltime_seconds` reads the value. One function, because every caller that
    wants a deadline wants both and doing it by hand is how a page ends up
    parsing `-t` slightly differently from the form that wrote it.
    """
    if srun is None:
        return None
    return walltime_seconds(split_srun(srun).get("walltime"))


def join_srun(extra, walltime="", cores="", memory="") -> str:
    """The three boxes spliced back into a site's own arguments.

    Splicing rather than concatenating: somebody who asked for eight hours has
    not thereby said anything about the partition, and a value already present
    in `extra` is replaced in place so the line cannot end up naming `-t`
    twice. An empty box leaves the flag alone -- that is "whatever the site
    does", which is a real answer and not the same as zero.
    """
    line = extra or ""
    for flag, value in (("-t", walltime), ("-c", cores), ("--mem", memory)):
        line = _with_flag(line, flag, value)
    return line


@dataclass(frozen=True)
class Recipe:
    """One site, and what connecting to it takes."""

    id: str
    label: str
    #: One sentence, in the second person, saying who this is for. Read on
    #: the card in the catalogue, where somebody is CHOOSING.
    blurb: str
    #: Two or three words naming the kind of machine, for the card of a server
    #: already saved. A different job from `blurb`, which is why it is a
    #: different field: that card is a status readout for somebody who chose
    #: months ago, and what it owes them is "which of my machines is this",
    #: not the sales pitch and not the address. Falls back to `label`.
    summary: str = ""
    #: The ssh target with `{user}` where the username goes. A recipe with no
    #: template asks for the whole address instead -- the generic SSH case.
    target_template: str = "{user}@{host}"
    #: srun arguments, or None for a host that runs Plexora directly. The empty
    #: string is meaningful and distinct from None -- see Remote.srun.
    srun: str | None = None
    #: Forward from the login node instead of ssh-ing into the compute node.
    #: True only for a site known to refuse the second hop.
    bind_node: bool = False
    #: How to invoke Plexora over there, when the site needs more than
    #: `plexora` -- by a wide margin the commonest reason a connection fails.
    remote_command: str = "plexora"
    #: Which boxes the form shows. `user` almost always; `host` when the
    #: address is not knowable from here.
    ask: tuple = (ASK_USER,)
    #: Anything a person should read before pressing Connect. Rendered as
    #: sentences under the form, not as a tooltip: on a first connection these
    #: are the difference between a working setup and an afternoon.
    notes: tuple = ()
    #: Whether this names a particular institution's cluster, as opposed to
    #: describing a shape ("any Slurm cluster", "any ssh host"). Only a site
    #: preset asserts facts about somebody else's machine, so only a site
    #: preset can be wrong about them -- which is what `tested` is about.
    site: bool = False
    #: Whether this site's values have been seen to work, rather than read off
    #: a documentation page. Meaningless for a generic shape, where there is
    #: nothing site-specific to have got wrong.
    tested: bool = False
    #: Whether this is one named organisation's cluster, rather than a kind of
    #: machine anyone can have. Not the same question as `site`, which the two
    #: cloud presets also answer yes to: an AWS instance is a shape -- everyone
    #: can launch one -- while O2 is a shape only if you have an HMS account.
    #: The catalogue offers the shapes and keeps these behind a second click,
    #: so that the first screen fits everybody.
    institution: bool = False
    #: Anything this preset needs that the vocabulary above has no word for.
    #: Empty for all but one of them. `flow` is the only key with meaning to
    #: the form -- see the module docstring -- and the rest of a flow recipe's
    #: `extra` is the catalogues its boxes are drawn from, which ride down with
    #: the recipe rather than costing a route of their own.
    extra: dict = field(default_factory=dict)

    @property
    def flow(self) -> str:
        """Which bespoke form this preset wants, or "" for the standard one."""
        return str(self.extra.get("flow") or "")

    @property
    def srun_extra(self) -> "str | None":
        """This site's job options minus the three the form has boxes for."""
        if self.srun is None:
            return None
        return _without_flags(self.srun, [flag for flag, _ in MANAGED_FLAGS])

    @property
    def unverified(self) -> bool:
        """Whether to warn. What the badge renders from."""
        return self.site and not self.tested

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "blurb": self.blurb,
            # `or label` here rather than at every reader: a recipe with
            # nothing short to say still has to name itself on a saved card.
            "summary": self.summary or self.label,
            "target_template": self.target_template,
            "srun": self.srun,
            "srun_extra": self.srun_extra,
            # The same line as the boxes a form shows it in, so that the
            # Settings form filling itself in from a preset and the Settings
            # form filling itself in from a saved server take one code path.
            "srun_parts": split_srun(self.srun),
            "bind_node": self.bind_node,
            "remote_command": self.remote_command,
            "ask": list(self.ask),
            "notes": list(self.notes),
            "site": self.site,
            "tested": self.tested,
            "institution": self.institution,
            "unverified": self.unverified,
            # Serialised, unlike when this field was unused: it is how the form
            # learns which shape to draw, and how the Google Cloud form gets
            # its machine-type and region lists without a second request.
            "extra": dict(self.extra),
        }


#: In the order the surfaces offer them: the five that describe a SHAPE of
#: machine -- any ssh host, any Slurm cluster, a workstation, a cloud
#: account -- and then the named institutions, which the catalogue keeps
#: behind a second click. A shape fits everybody and asserts nothing about
#: anybody's cluster; a named site fits the few people who are at it, and
#: is the one kind of preset that can be wrong -- which is what `tested`
#: is about.
#:
#: Within the shapes this is the order of increasing commitment: an ssh
#: host you already have, then a scheduler, then a machine on your desk,
#: then two clouds that bill you. Offering "spend money in your own cloud
#: account" ahead of "the machine you already log into" is the wrong
#: first suggestion whether or not it has been tested.
RECIPES = (
    Recipe(
        id="ssh",
        label="A plain SSH server",
        summary="SSH server",
        blurb="Any host you can already ssh into — a lab server, a cloud VM. "
              "Plexora runs there directly.",
        srun=None,
        ask=(ASK_USER, "host"),
        notes=(
            "For your own desktop or a lab machine, “A workstation” below "
            "asks the same things and knows what Windows and macOS need.",
            "Plexora has to be installed on that machine and on your PATH "
            "when ssh runs a command without a login shell. If it is not, "
            "edit the saved server and give the full path to `plexora`.",
        ),
    ),
    Recipe(
        id="slurm",
        label="A Slurm cluster",
        summary="Slurm compute cluster",
        blurb="Any site with a login node and `srun`. Give the address and, "
              "if your site needs them, the job arguments.",
        # The empty string, not None: "use srun, with this site's defaults",
        # which is a real and common answer on a cluster whose partition
        # defaults are already right.
        srun="",
        ask=(ASK_USER, "host", ASK_WALLTIME, ASK_CORES, ASK_MEMORY),
        notes=(
            "Everything you type here is passed to `srun` verbatim, so use "
            "whatever partition and flags your site expects.",
            # The one thing this preset cannot guess, and the commonest way it
            # fails: `srun` with no `-p` works on a site with a default
            # partition and is refused outright on a site without one, and the
            # refusal arrives underneath the login banner.
            "Many clusters have no default partition. If the job is refused "
            "with “please specify partition with -p”, put `-p <name>` in the "
            "advanced scheduler arguments — `sinfo -s` on the login node "
            "lists them.",
            "If your cluster refuses ssh into a compute node, edit the saved "
            "server afterwards and turn on “forward from the login node”.",
        ),
    ),
    Recipe(
        id="workstation",
        label="A workstation",
        summary="Desktop or lab workstation",
        blurb="Your own desktop or a lab machine — Windows, macOS or Linux. "
              "Plexora uses its disks and its processors; nothing queues.",
        srun=None,
        # The two standard boxes plus one question. `host` because a
        # workstation's address is not knowable from here, and the operating
        # system because it decides how every command line is quoted -- see
        # connect.py's "remote operating systems".
        ask=(ASK_USER, "host", ASK_OS),
        notes=(
            "The machine needs an SSH server running and has to be reachable "
            "from here — on the same network, or through your institution's "
            "VPN. Plexora uses your system ssh, so whatever already works in "
            "a terminal works here.",
            "Plexora has to be installed on that machine. If it is not on the "
            "PATH a non-interactive ssh sees, give the environment instead — "
            "the full path to a conda environment, or `conda run -n NAME "
            "plexora`. Turn on “install or update Plexora” below and it will "
            "be installed into whichever you name.",
            "You choose which files to open afterwards, from the data fields "
            "themselves: switch one to Remote and browse that machine. "
            "Nothing has to be named in advance.",
            "A shared workstation is fine. Everyone who connects gets their "
            "own Plexora on its own port, under their own account, and "
            "disconnecting ends only their own.",
        ),
        extra={
            # The catalogue the form's OS control is drawn from, riding down
            # with the recipe rather than costing a route of its own -- the
            # same arrangement as the Google Cloud preset's machine types. The
            # per-OS prose is server-side for the same reason the notes are.
            "os_choices": [
                {"name": "windows", "label": "Windows",
                 "hint": "Needs OpenSSH Server, which Windows ships but does "
                         "not enable: Settings → System → Optional features → "
                         "Add → OpenSSH Server, then start the “OpenSSH SSH "
                         "Server” service and set it to Automatic."},
                {"name": "macos", "label": "macOS",
                 "hint": "Turn on System Settings → General → Sharing → "
                         "Remote Login."},
                {"name": "linux", "label": "Linux",
                 "hint": "Needs sshd running, which most distributions "
                         "install and enable already."},
            ],
            "default_os": "linux",
        },
    ),
    Recipe(
        id="gcloud",
        label="Google Cloud (GCP)",
        summary="Google Cloud VM",
        blurb="Your images are in a Cloud Storage bucket. Plexora mounts it on "
              "a Compute Engine VM — one it starts for you, or one you already "
              "run — and connects.",
        # Neither is used: a Google Cloud connection's address is a VM that
        # does not exist yet, and `_compose_gcloud` derives it from the name
        # the user gives the connection. Nothing is asked in the standard
        # vocabulary either -- see FLOW_GCLOUD.
        target_template="",
        ask=(),
        srun=None,
        # The environment the VM's first connection builds for itself. The
        # same field every other preset uses to say how to reach Plexora over
        # there, and here it also names what the install switch would upgrade.
        remote_command="~/plexora-venv",
        notes=(
            "Everything below is created in YOUR Google Cloud account and "
            "billed to it — a running VM costs money until it is stopped or "
            "deleted.",
            "You need the Google Cloud CLI installed on this machine, a "
            "project with billing enabled, and the Compute Engine API turned "
            "on in it.",
            "Nothing on the internet can reach the VM. Plexora goes in "
            "through Google's IAP tunnel and adds a firewall rule that "
            "refuses every other inbound connection, so this account needs "
            "the “IAP-secured Tunnel User” role as well as permission to "
            "create VMs and firewall rules.",
            "A new VM is asked for as a Spot instance by default — the same "
            "hardware at a large discount, which Google may reclaim at any "
            "time. Plexora asks for it to be stopped rather than deleted if "
            "that happens, so reconnecting brings the same machine back. "
            "Choose Standard if a session must not be interrupted.",
            "You choose what happens when Plexora exits: leave the VM "
            "running, stop it, or delete it. A stopped VM still bills for its "
            "disk — about $2 a month at 20 GB — until it is deleted, and it "
            "also shuts itself down if it is left with nobody connected.",
            "Point this at a VM you already run instead, and Plexora will "
            "only mount the bucket and connect: it never creates, changes the "
            "network of, or deletes a machine it did not make.",
            "Deleting the VM afterwards never deletes your bucket or anything "
            "in it. Plexora has no way to delete storage at all.",
        ),
        site=True,
        # Run end to end against a real account: a VM created, a bucket
        # mounted, Plexora installed on it and connected to. So no badge --
        # the badge means "we have not done this", and we have.
        tested=True,
        extra={
            "flow": FLOW_GCLOUD,
            # The catalogues the bespoke form draws its dropdowns from. They
            # ride down with the recipe rather than costing a route of their
            # own, and they are curated lists rather than a live query for the
            # reason `plexora.gcloud` gives: `machine-types list` returns
            # hundreds of rows per zone and a picker with everything in it is
            # a picker nobody can choose from.
            "machine_types": gcloud.machine_types(),
            "default_machine_type": gcloud.DEFAULT_MACHINE_TYPE,
            "regions": gcloud.regions(),
            "default_region": gcloud.DEFAULT_REGION,
            "mount_path": gcloud.DEFAULT_MOUNT_PATH,
            "boot_disk_gb": gcloud.DEFAULT_BOOT_DISK_GB,
            "idle_shutdown_minutes": gcloud.IDLE_SHUTDOWN_MINUTES,
            "provisioning_models": gcloud.provisioning_models(),
            "default_provisioning": gcloud.DEFAULT_PROVISIONING,
            # The three endings, each with the sentence that says what it
            # costs. Server-side like every other word on this form: what
            # "Delete VM" actually does is a fact about `plexora.gcloud`, and
            # a copy of it written into the browser would be a second place for
            # it to go out of date.
            "exit_actions": gcloud.exit_actions(),
            "default_exit": gcloud.DEFAULT_EXIT,
            "vm_sources": [
                {"name": gcloud.VM_PLEXORA,
                 "label": "Create a new VM",
                 "hint": "Plexora asks Google for a machine, mounts your "
                         "bucket on it, and gives it back when you are "
                         "finished. It is created, started, stopped and "
                         "deleted by Plexora."},
                {"name": gcloud.VM_EXISTING,
                 "label": "Use an existing VM",
                 "hint": "A machine you already run. Plexora mounts the "
                         "bucket on it and connects — it never creates one, "
                         "never changes its network, and never deletes it."},
            ],
        },
    ),
    # Shaped from documentation rather than from a session -- the only
    # one left that is. The Google Cloud preset above was once here too.
    Recipe(
        id="aws",
        label="An AWS EC2 instance",
        summary="Amazon EC2 instance",
        blurb="An instance you have already launched, with your key in your "
              "ssh config. Plexora runs on it directly.",
        srun=None,
        ask=(ASK_USER, "host"),
        notes=(
            "Untested by us. Give the public DNS name or the alias from your "
            "~/.ssh/config; Plexora uses your system ssh, so whatever already "
            "works in a terminal works here.",
            "The username is the AMI's, not yours: `ec2-user` on Amazon "
            "Linux, `ubuntu` on Ubuntu images.",
            "Nothing is opened to the internet — the connection is an ssh "
            "tunnel, so the instance's security group needs port 22 and "
            "nothing else.",
        ),
        site=True,
    ),
    Recipe(
        id="hms-o2",
        label="HMS O2",
        summary="Harvard O2 compute cluster",
        blurb="Harvard Medical School's cluster. Runs Plexora inside an "
              "interactive job, which is what the site expects.",
        target_template="{user}@o2.hms.harvard.edu",
        # Verified in DEPLOYMENT.md against a real session: the login node is
        # the target, srun gets the job, and the second hop into the compute
        # node works because O2 allows it via pam_slurm_adopt.
        srun=DEFAULT_SRUN,
        ask=(ASK_USER, ASK_WALLTIME, ASK_CORES, ASK_MEMORY),
        notes=(
            "Connect to the LOGIN node — o2.hms.harvard.edu. Plexora asks the "
            "scheduler for a compute node itself.",
            "Queueing is normal and is not a failure. The interactive "
            "partition is usually seconds; a busy one can be minutes.",
            "Your walltime is how long the connection can last. The job ends "
            "when you disconnect, or when the time runs out.",
        ),
        site=True,
        tested=True,
        institution=True,
    ),
    Recipe(
        id="mgb-eris",
        label="MGB / BWH ERIS",
        summary="MGB research compute cluster",
        blurb="Mass General Brigham's research cluster, ERISTwo. Slurm, so "
              "Plexora asks for an interactive job.",
        # ERISOne, and the erisone.partners.org address this preset used to
        # carry, are retired. eris2n7 and eris2n8 are the login nodes people
        # are given now; either one works, and the target is editable
        # afterwards, so pinning one is better than a round-robin alias that
        # could put the job connection and the tunnel on different hosts.
        target_template="{user}@eris2n7.research.partners.org",
        srun=DEFAULT_SRUN,
        ask=(ASK_USER, ASK_WALLTIME, ASK_CORES, ASK_MEMORY),
        notes=(
            # First, because it is the one failure that looks like a broken
            # preset and is not: with the VPN down ssh does not get refused,
            # it gets nothing, and the connection dies at the first step with
            # nothing on the far side to have said why.
            "MGB remote connections require an active VPN connection. If the "
            "VPN is not connected, the SSH connection will fail.",
            "eris2n7 and eris2n8.research.partners.org are interchangeable "
            "login nodes. If one is down, edit the target and use the other.",
            "ERISTwo runs Slurm; a job line a colleague wrote for the old LSF "
            "queues does not apply here. If the interactive partition refuses "
            "this much memory, ask for less, or put `-p bigmem` in the "
            "advanced scheduler box instead.",
        ),
        site=True,
        tested=True,
        institution=True,
    ),
)


def all_recipes() -> tuple:
    return RECIPES


def find(recipe_id: str) -> "Recipe | None":
    for recipe in RECIPES:
        if recipe.id == recipe_id:
            return recipe
    return None


def split_target(recipe, target) -> dict:
    """One stored ssh target, as the boxes this recipe's form shows it in.

    The inverse of `Recipe.target_template`, and here rather than in the
    browser for the reason `split_srun` is: the page that SHOWS a username and
    the route that STORES one must not disagree about which half of an address
    is which.

    Only the parts the template names as holes are read out of the record. A
    template with a fixed host -- `{user}@o2.hms.harvard.edu` -- gives its own
    host back, because that is the fact the preset is asserting and not a box
    anybody filled in. An address with no `@` is all host: a profile written by
    hand, or by `plexora connect --save` against an ssh config alias, has no
    username in it, and splitting one out of the template would put somebody
    else's account in the box.
    """
    text = str(target or "").strip()
    template = getattr(recipe, "target_template", "") or ""
    if "@" in text:
        user, _, host = text.rpartition("@")
    else:
        user, host = "", text
    if "{host}" not in template:
        fixed = template.format(user="", host="").lstrip("@")
        host = fixed or host
    return {"user": user, "host": host}


def for_remote(remote) -> str:
    """Which preset's form edits this saved profile.

    Editing goes through the catalogue that adding goes through -- there is one
    form and it is the recipe's -- so every profile has to name a recipe,
    including every one written before a profile recorded which preset made it.
    A profile composed since then says so itself; for the rest the shape is
    read back off the record.

    Most specific first: what the profile says about itself, then the two
    records only a preset could have written, then a site whose address the
    template spells out, then the two generic shapes, told apart by whether
    there is a scheduler. It never answers "" -- `ssh` fits any host and is
    what "no idea" looks like.
    """
    host = split_target(None, getattr(remote, "target", ""))["host"].lower()
    stored = find(str(getattr(remote, "recipe", "") or ""))
    # The recipe it was composed from, unless what that recipe asserts about
    # the address is no longer true of the profile. A preset with a fixed host
    # has no box for one, so reopening the wrong site's form and saving it
    # would move a hand-edited profile back to a cluster it had been pointed
    # away from -- silently, since nothing on that form shows the address.
    if stored is not None and (
            "{host}" in stored.target_template
            or not host
            or host == split_target(stored, "")["host"].lower()):
        return stored.id
    if getattr(remote, "gcloud", None):
        return "gcloud"
    if getattr(remote, "workstation", None):
        return "workstation"
    for recipe in RECIPES:
        if not recipe.site or "{host}" in recipe.target_template:
            continue
        if host and host == split_target(recipe, "")["host"].lower():
            return recipe.id
    return "slurm" if getattr(remote, "srun", None) is not None else "ssh"


def compose(recipe_id: str, answers) -> dict:
    """The `POST /settings/remotes` body this recipe and these answers make.

    Deliberately returns the same shape the Settings form posts, and goes
    through the same route: a recipe is a filled-in form, not a second way to
    write a profile. Anything it cannot know -- and a credential is the whole
    of that -- is simply absent.
    """
    recipe = find(recipe_id)
    if recipe is None:
        raise KeyError(recipe_id)
    if recipe.flow == FLOW_GCLOUD:
        return _compose_gcloud(recipe, answers)

    # The switches are read off the raw answers and the boxes off the trimmed
    # ones: `str(False or "")` is "" and `str(True or "")` is "True", so a
    # boolean that went through the text pass would arrive as a string that is
    # true either way in one direction and empty in the other.
    raw = dict(answers or {})
    answers = {k: str(v or "").strip() for k, v in raw.items()}
    user = answers.get("user", "")
    host = answers.get("host", "")
    # Both are refused here rather than left to produce a target like
    # "@o2.hms.harvard.edu" or "you@". The second would be an ssh that
    # silently used the laptop's own username, which on a cluster is somebody
    # else's account or nobody's, and fails as "Permission denied" -- the one
    # error message that sends people looking for the wrong problem.
    if ASK_USER in recipe.ask and not user:
        raise ValueError("Enter your username on that machine.")
    if "host" in recipe.ask and not host:
        raise ValueError("Enter the address to connect to, "
                         "e.g. login.cluster.edu.")
    target = recipe.target_template.format(user=user, host=host)

    srun = recipe.srun
    if srun is not None:
        # Advanced, when the form sent it: the box holds this site's options
        # MINUS the three that have boxes of their own (Recipe.srun_extra), so
        # what arrives here is the partition and whatever else the site needs.
        # Membership rather than truthiness -- an empty box is a person saying
        # "no extra flags", which is different from a form that never asked.
        if "srun" in answers:
            srun = answers["srun"]
        # The three knobs a person actually turns, spliced in by the same
        # function the Settings form's Cores / Memory / Time boxes go through.
        srun = join_srun(srun,
                         walltime=answers.get("walltime", ""),
                         cores=answers.get("cores", ""),
                         memory=answers.get("memory", ""))

    body = {
        "name": answers.get("name") or recipe.id,
        "target": target,
        # By a wide margin the commonest reason a connection fails is that ssh
        # cannot find `plexora`, and it is the one thing a preset cannot know
        # about somebody's account. Overridable here so it can be fixed before
        # the first attempt rather than after it.
        "remote_command": (answers.get("remote_command", "").strip()
                           or recipe.remote_command),
        "use_srun": srun is not None,
        "srun": srun or "",
        # The recipe that composed this, so that editing the profile later
        # reopens the form it was filled in on rather than guessing at one.
        # Under its own key at the top level and stored in `extra`, the same
        # seam `gcloud` and `workstation` ride through.
        "recipe": recipe.id,
        # Whether the second hop into the compute node works is a fact about
        # the site, and that is what the preset is for -- but it is on the form
        # too, because a site that refuses it for one account and not another
        # is a real thing and the preset cannot be right about both.
        "bind_node": (bool(raw["bind_node"]) if "bind_node" in raw
                      else recipe.bind_node),
        # Always present, never defaulted on. No preset asserts that somebody
        # wants software installed into their account on a machine we have
        # only ever read documentation about -- and on a shared cluster the
        # environment a bare `plexora` resolves to is quite often a site
        # install nobody connecting from here owns. It is a switch on the
        # form, next to the field that names the environment it would write
        # to, and it is off until it is turned on.
        "install": bool(raw.get("install")),
    }
    # Two answers no preset can know, from the boxes the form grew when the
    # hand-written Settings form was retired into it: where the data sits on
    # that machine, and any extra port to carry through the tunnel. Passed
    # through only when the form actually sent them -- `_remote_payload` reads
    # membership, and a key that is absent is a caller that never asked, whose
    # saved answer has to survive the edit. `forwards` goes through untrimmed
    # because it is a list and the text pass above would stringify it.
    if "data_dir" in raw:
        body["data_dir"] = answers.get("data_dir", "")
    if "forwards" in raw:
        body["forwards"] = raw["forwards"]
    if ASK_OS in recipe.ask:
        # Validated here rather than trusted, for the reason every other answer
        # on this form is: what arrives is whatever was posted, and this one
        # ends up deciding how a command line is quoted. An unrecognised value
        # would be read as POSIX by everything downstream -- silently right for
        # Linux and macOS, silently wrong for the one machine that needed the
        # question asked.
        system = answers.get(ASK_OS, "").lower() \
            or str((recipe.extra or {}).get("default_os") or "")
        if system not in WORKSTATION_OS:
            raise ValueError("Choose the workstation's operating system: "
                             + ", ".join(WORKSTATION_OS) + ".")
        # Under its own key rather than at the top level, so it lands in the
        # profile's `extra` the way the Google Cloud record does -- see
        # `Remote.workstation` for why that seam and not a new column.
        body["workstation"] = {"os": system}
    return body


def _compose_gcloud(recipe, answers) -> dict:
    """The same `POST /settings/remotes` body, for the preset with no machine.

    Every other recipe fills in facts about a cluster somebody already has.
    This one describes one that does not exist yet -- so what it validates is
    a request rather than an address, and the two things it will not proceed
    without are the two that decide where the machine goes: **a project, and a
    bucket.** The bucket is required rather than optional because the whole
    premise is inverted here. The data is what the user has; the VM is a thing
    Plexora rents to read it. A connection with no bucket would start a machine
    with nothing on it, bill somebody for it, and open a viewer onto an empty
    directory.

    The result is an ordinary profile with an ordinary target, and everything
    Google-specific rides in one `gcloud` key that `_remote_payload` puts under
    `extra`. There is no credential in it -- see `plexora.gcloud.profile`.
    """
    raw = dict(answers or {})
    answers = {k: str(v or "").strip() for k, v in raw.items()}

    name = answers.get("name") or recipe.id
    project = answers.get("project")
    bucket = answers.get("bucket")
    if not project:
        raise ValueError("Choose the Google Cloud project your data is in.")
    if not bucket:
        raise ValueError(
            "Name the Cloud Storage bucket your images are in. Plexora mounts "
            "it on the VM and reads from there, so there is nothing to connect "
            "to without one.")
    if not gcloud.valid_bucket_name(bucket):
        raise ValueError(
            f"“{bucket}” is not a Cloud Storage bucket name. Bucket names are "
            "lower-case letters, digits, dashes, underscores and dots.")

    # Whose machine this is, resolved before the region and the zone because it
    # changes which of those two is allowed to decide the other. See the
    # ownership paragraph in `plexora.gcloud`. Anything but the explicit
    # "existing" means Plexora provides it, so a profile written before this
    # field existed keeps its old meaning.
    vm_source = (answers.get("vm_source") or gcloud.VM_PLEXORA)
    if vm_source not in (gcloud.VM_PLEXORA, gcloud.VM_EXISTING):
        vm_source = gcloud.VM_PLEXORA
    if vm_source == gcloud.VM_EXISTING:
        vm_name = answers.get("vm_name", "")
        if not vm_name:
            raise ValueError(
                "Name the VM you want to use. Plexora will not create one for "
                "this connection, so it needs to know which existing machine "
                "to connect to.")
        if not gcloud.valid_instance_name(vm_name):
            raise ValueError(
                f"“{vm_name}” is not a Compute Engine instance name. They "
                "start with a lower-case letter and contain only lower-case "
                "letters, digits and dashes.")
    else:
        # Derived rather than stored: the reuse ladder looks the instance up by
        # name every time, so there is no instance id to keep in step.
        vm_name = gcloud.instance_name(name)

    location = answers.get("bucket_location", "")
    region = answers.get("region") or gcloud.region_for_bucket_location(location)[0]
    if not gcloud.valid_region(region):
        raise ValueError(
            f"“{region}” is not a Google Cloud region. They are spelled like "
            "us-east1 or europe-west4 — no dash before the number.")

    zone = answers.get("zone", "")
    if zone and not gcloud.valid_zone(zone):
        raise ValueError(
            f"“{zone}” is not a Google Cloud zone. A zone is a region and a "
            f"letter, like {region}-b.")
    if zone and gcloud.region_of_zone(zone) != region:
        if vm_source == gcloud.VM_EXISTING:
            # Where somebody's own machine lives is a fact, not a preference.
            # Refusing it would be refusing the only zone that can possibly be
            # right -- so the VM's zone wins and the region follows it. Being
            # far from the data is real and costs egress, which is what the
            # form's mismatch warning is for; it is not an error.
            region = gcloud.region_of_zone(zone)
        else:
            raise ValueError(
                f"The zone {zone} is not in {region}. Pick a zone in that "
                "region, or change the region.")
    if not zone and vm_source == gcloud.VM_EXISTING:
        # Ask Google where the machine actually is, rather than guessing a zone
        # in the bucket's region and describing a VM that is not there. This is
        # what makes typing a bare name enough: somebody who knows their VM is
        # called `analysis-box` should not have to know which zone they put it
        # in eighteen months ago.
        try:
            zone = gcloud.zone_of_instance(project, vm_name)
        except gcloud.GcloudError:
            zone = ""
        if not zone:
            raise ValueError(
                f"Plexora could not find a VM called “{vm_name}” in "
                f"{project}. Check the name, or say which zone it is in.")
        region = gcloud.region_of_zone(zone) or region
    if not zone:
        # The form normally sends one, because it asked Google which zones are
        # up while the user was still filling the boxes in. Resolving it here
        # too means a saved profile always names a zone -- and a guess would
        # be wrong for the commonest region there is: us-east1 has no zone a.
        try:
            zone = gcloud.pick_zone(project, region)
        except gcloud.GcloudError:
            zone = ""
    if not zone:
        raise ValueError(
            f"Plexora could not find a zone in {region} to start a VM in. "
            "Pick one under Advanced, or choose another region.")

    # Not checked against the curated list: the form offers a box for a type
    # that list does not name, which is what makes a GPU type, a C3 or a
    # `custom-4-8192` reachable at all. What IS checked is the shape, so a
    # typo is a sentence here rather than a Compute Engine error four screens
    # into provisioning.
    machine_type = answers.get("machine_type") or gcloud.DEFAULT_MACHINE_TYPE
    if not gcloud.valid_machine_type(machine_type):
        raise ValueError(
            f"“{machine_type}” is not a Compute Engine machine type. They are "
            "spelled like e2-highmem-16, n2-standard-8 or custom-4-8192.")
    provisioning = (answers.get("provisioning_model")
                    or gcloud.DEFAULT_PROVISIONING)
    if not gcloud.valid_provisioning(provisioning):
        raise ValueError(
            "A VM is either Spot or Standard. Choose one of the two.")
    # Absent means the default rather than "leave it running", and the default
    # is the one that stops billing: a form that failed to send this field must
    # not be the reason a 16-core machine runs all weekend. `gcloud.profile`
    # refuses Delete for a machine the user already runs, whatever arrives here.
    on_exit = answers.get("on_exit") or gcloud.DEFAULT_EXIT
    if not gcloud.valid_exit(on_exit):
        raise ValueError(
            "Choose what should happen to the VM when Plexora exits: leave it "
            "running, stop it, or delete it.")
    mount_path = answers.get("mount_path") or gcloud.DEFAULT_MOUNT_PATH
    if not gcloud.valid_mount_path(mount_path):
        raise ValueError(
            "Where to mount the bucket has to be a plain path, like "
            "~/plexora-data.")
    try:
        boot_disk = int(answers.get("boot_disk_gb")
                        or gcloud.DEFAULT_BOOT_DISK_GB)
    except ValueError:
        raise ValueError("Boot disk size is a number of gigabytes, "
                         f"e.g. {gcloud.DEFAULT_BOOT_DISK_GB}.") from None
    if boot_disk < gcloud.MIN_BOOT_DISK_GB:
        raise ValueError(f"A boot disk under {gcloud.MIN_BOOT_DISK_GB} GB is "
                         "not enough for Plexora and its dependencies.")

    return {
        "name": name,
        "target": vm_name,
        "remote_command": (answers.get("remote_command")
                           or recipe.remote_command),
        # No scheduler, and there never will be one here: a VM Plexora asked
        # for is already the machine the work belongs on.
        "use_srun": False,
        "srun": "",
        "bind_node": False,
        "install": bool(raw.get("install")),
        "recipe": recipe.id,
        # **The mount IS the data root.** That is the whole point of the
        # preset -- the bucket the user chose is where their projects and
        # figures live for the session, not a folder they then have to go
        # looking for.
        "data_dir": mount_path,
        **({"forwards": raw["forwards"]} if "forwards" in raw else {}),
        "gcloud": gcloud.profile(
            account=answers.get("account", ""),
            project=project,
            bucket=bucket,
            bucket_location=location,
            region=region,
            zone=zone,
            machine_type=machine_type,
            provisioning_model=provisioning,
            vm_name=vm_name,
            vm_source=vm_source,
            mount_path=mount_path,
            boot_disk_gb=boot_disk,
            service_account=answers.get("service_account", ""),
            on_exit=on_exit,
            # Absent means on. A VM with no way out cannot install
            # gcsfuse or Plexora and so cannot connect at all, and switching
            # this off is only correct on a network that already has Cloud
            # NAT -- which is a thing somebody knows about their own project,
            # never a thing to assume from silence.
            external_ip=("external_ip" not in raw
                         or bool(raw.get("external_ip"))),
            idle_shutdown_minutes=_idle_minutes(raw),
        ),
    }


def _idle_minutes(raw):
    """How long the VM may sit unused before it shuts itself down.

    Blank or absent means the default rather than "never": this is the only
    protection that survives the laptop being shut, so switching it off has to
    be somebody typing a zero on purpose.
    """
    from plexora import gcloud

    if "idle_shutdown_minutes" not in raw:
        return gcloud.IDLE_SHUTDOWN_MINUTES
    text = str(raw.get("idle_shutdown_minutes") or "").strip()
    if not text:
        return gcloud.IDLE_SHUTDOWN_MINUTES
    try:
        minutes = int(text)
    except ValueError:
        raise ValueError(
            "Idle shutdown is a number of minutes, e.g. "
            f"{gcloud.IDLE_SHUTDOWN_MINUTES}. Use 0 to switch it off.") from None
    if minutes < 0:
        raise ValueError("Idle shutdown cannot be a negative number of "
                         "minutes. Use 0 to switch it off.")
    return minutes


def _without_flags(arguments, flags):
    """`arguments` with each of `flags` and its value removed.

    The inverse of `_with_flag`, and deliberately next to it: the two have to
    agree about what a flag's value looks like, and they will only keep
    agreeing if changing one puts the other in front of you.
    """
    parts = (arguments or "").split()
    out = []
    index = 0
    while index < len(parts):
        if parts[index] in flags:
            index += 2      # the flag and the value that follows it
            continue
        out.append(parts[index])
        index += 1
    return " ".join(out).strip()


def _with_flag(arguments, flag, value):
    """`arguments` with `flag`'s value replaced, or appended, or left alone."""
    if not value:
        return arguments
    parts = arguments.split()
    out = []
    index = 0
    replaced = False
    while index < len(parts):
        if parts[index] == flag:
            out += [flag, value]
            replaced = True
            index += 2      # skip the value that was there
            continue
        out.append(parts[index])
        index += 1
    if not replaced:
        out += [flag, value]
    return " ".join(out).strip()
