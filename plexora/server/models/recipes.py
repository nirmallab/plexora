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
them. Only HMS O2 and MGB ERISTwo are pinned to observed behaviour (see
DEPLOYMENT.md, which quotes a real O2 session); the rest are shaped from
published documentation and carry `site=True, tested=False`, which the form
renders as a badge. Presenting a guess with the same confidence as a verified
fact is how somebody spends an afternoon on a partition name that never
existed.

The generic shapes -- "a Slurm cluster", "a plain SSH server" -- assert nothing
about any particular machine, because the user supplies the address. They carry
no badge, and there is nothing about them to test.

**Nothing here wraps a job in anything but `srun`.** Plexora's scheduler
support IS `srun` (connect.srun_command_line), so an LSF or PBS site gets the
plain-SSH shape and a note telling them to get their own interactive session
first. Offering an `bsub` box that quietly did nothing would be worse than not
offering one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


#: What a recipe may ask for beyond the username, keyed by the field it fills.
#: Kept small on purpose: every extra box is a question somebody has to have an
#: opinion about before they can press Connect, and the whole point is that the
#: site's defaults are usually right.
ASK_USER = "user"
ASK_WALLTIME = "walltime"
ASK_CORES = "cores"
ASK_MEMORY = "memory"

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
    #: One sentence, in the second person, saying who this is for.
    blurb: str
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
    extra: dict = field(default_factory=dict)

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
            "unverified": self.unverified,
        }


#: In the order the form offers them: the sites whose values are verified,
#: then the two generic shapes that fit any cluster or any host, then the
#: untested site presets. A named site somebody recognises is worth more than a
#: generic label, and an untested one is worth less than either -- which is
#: exactly the order below.
RECIPES = (
    Recipe(
        id="hms-o2",
        label="HMS O2",
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
    ),
    Recipe(
        id="mgb-eris",
        label="MGB-ERIS",
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
    ),
    Recipe(
        id="slurm",
        label="A Slurm cluster",
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
            "If your cluster refuses ssh into a compute node, edit the saved "
            "server afterwards and turn on “forward from the login node”.",
        ),
    ),
    Recipe(
        id="ssh",
        label="A plain SSH server",
        blurb="A workstation, a lab server, or a cloud VM you can already ssh "
              "into. Plexora runs there directly.",
        srun=None,
        ask=(ASK_USER, "host"),
        notes=(
            "Plexora has to be installed on that machine and on your PATH "
            "when ssh runs a command without a login shell. If it is not, "
            "edit the saved server and give the full path to `plexora`.",
        ),
    ),
    # -- shaped from documentation, not from a session ----------------------
    Recipe(
        id="aws",
        label="An AWS EC2 instance",
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
        id="gcloud",
        label="A Google Cloud VM",
        blurb="A Compute Engine instance you can already reach over ssh. "
              "Plexora runs on it directly.",
        srun=None,
        ask=(ASK_USER, "host"),
        notes=(
            "Untested by us. Plexora uses your system ssh rather than "
            "`gcloud compute ssh`, so run `gcloud compute config-ssh` first "
            "and use the host alias it writes into ~/.ssh/config.",
            "The tunnel needs port 22 only; no firewall rule for Plexora's "
            "own port is required.",
        ),
        site=True,
    ),
)


def all_recipes() -> tuple:
    return RECIPES


def find(recipe_id: str) -> "Recipe | None":
    for recipe in RECIPES:
        if recipe.id == recipe_id:
            return recipe
    return None


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
        "bind_node": recipe.bind_node,
        # Always present, never defaulted on. No preset asserts that somebody
        # wants software installed into their account on a machine we have
        # only ever read documentation about -- and on a shared cluster the
        # environment a bare `plexora` resolves to is quite often a site
        # install nobody connecting from here owns. It is a switch on the
        # form, next to the field that names the environment it would write
        # to, and it is off until it is turned on.
        "install": bool(raw.get("install")),
    }
    return body


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
