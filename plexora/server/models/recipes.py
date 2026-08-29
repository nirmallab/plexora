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
them. Only HMS O2 is pinned to observed behaviour (see DEPLOYMENT.md, which
quotes a real session); the rest are shaped from published documentation and
carry `site=True, tested=False`, which the form renders as a badge. Presenting
a guess with the same confidence as a verified fact is how somebody spends an
afternoon on a partition name that never existed.

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
ASK_MEMORY = "memory"


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
            "bind_node": self.bind_node,
            "remote_command": self.remote_command,
            "ask": list(self.ask),
            "notes": list(self.notes),
            "site": self.site,
            "tested": self.tested,
            "unverified": self.unverified,
        }


#: In the order the form offers them: the one site whose values are verified,
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
        srun="-p interactive -t 4:00:00 --mem 16G",
        ask=(ASK_USER, ASK_WALLTIME, ASK_MEMORY),
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
        id="slurm",
        label="A Slurm cluster",
        blurb="Any site with a login node and `srun`. Give the address and, "
              "if your site needs them, the job arguments.",
        # The empty string, not None: "use srun, with this site's defaults",
        # which is a real and common answer on a cluster whose partition
        # defaults are already right.
        srun="",
        ask=(ASK_USER, "host", ASK_WALLTIME, ASK_MEMORY),
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
        id="bwh-eris",
        label="BWH ERISOne",
        blurb="Brigham and Women's research cluster. Slurm, so Plexora asks "
              "for an interactive job.",
        target_template="{user}@erisone.partners.org",
        srun="-p interactive -t 4:00:00 --mem 16G",
        ask=(ASK_USER, ASK_WALLTIME, ASK_MEMORY),
        notes=(
            "Untested by us: the address and partition come from the site's "
            "documentation rather than from a connection we have made. If "
            "they are wrong, edit the saved server — nothing here is fixed.",
            "ERISOne requires the Partners VPN or an on-campus network before "
            "ssh will reach it at all.",
        ),
        site=True,
    ),
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

    answers = {k: str(v or "").strip() for k, v in dict(answers or {}).items()}
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
        # The two knobs a person actually turns, spliced into the site's own
        # arguments rather than replacing them: somebody who wants eight hours
        # has not thereby said anything about the partition.
        srun = _with_flag(srun, "-t", answers.get("walltime", ""))
        srun = _with_flag(srun, "--mem", answers.get("memory", ""))

    body = {
        "name": answers.get("name") or recipe.id,
        "target": target,
        "remote_command": recipe.remote_command,
        "use_srun": srun is not None,
        "srun": srun or "",
        "bind_node": recipe.bind_node,
    }
    return body


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
