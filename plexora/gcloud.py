"""Google Cloud, as much of it as one connection needs and no more.

The premise of the Google Cloud preset is the opposite way round from every
other one: the machine is not the thing somebody has, the DATA is. Images live
in a Cloud Storage bucket, and a VM is a thing Plexora asks for in order to
read them -- rented for the session, mounted onto the bucket, and given back.
So this module's job is the four verbs that premise needs: which projects and
buckets you have, where a bucket lives, and how to get a VM standing next to
it.

**Everything goes through the `gcloud` CLI.** No google-cloud-* dependency, no
service-account key file, no OAuth of our own -- `gcloud auth login` already
does the browser flow, already stores the refresh token in the user's own
credential store, and is already what a Google Cloud user has. Plexora reads
`gcloud auth list` to find out who is signed in and never sees a token.
**Nothing in Plexora stores a Google credential**, and the wrapper here has no
field to put one in, for the same reason `remotes.py` has none for a password.

**There is no storage-deletion verb in this file, and there must never be
one.** Deleting the VM is a normal end to a session -- the compute is rented,
the data is not -- and the one way that could become a catastrophe is a wrapper
that knew how to remove a bucket. It does not. Everything below either reads
storage or names it to gcsfuse; the only destructive verb is
`delete_instance`, whose argv cannot mention the bucket at all.

**Who owns the machine decides what may be done to it.** A profile is either
renting its VM from Plexora (`vm_source="plexora"`) or pointing at one the user
already runs (`vm_source="existing"`), and that single field settles four
questions at once: whether a missing VM is created or an error, whether it is
stopped when nobody is connected, whether its network may be altered, and
whether the delete verb is offered at all. A rented machine is Plexora's to
clean up and is cleaned up eagerly, because the alternative is somebody's bill.
A machine the user already had is never created, never automatically stopped,
never tagged or given an address, and -- enforced by a label check in
`delete_instance` rather than by the caller remembering -- can never be
deleted through Plexora at all.

**Nothing on the internet reaches a Plexora VM, and that is arranged twice
over.** The way in is Google's Identity-Aware Proxy, and `DENY_RULE` refuses
everything else at the firewall. What a rented VM does get is a way *out* -- an
address, because a Compute Engine instance created with `--no-address` has no
outbound route at all unless the network was built to give it one, and a VM
that cannot reach `packages.cloud.google.com` or PyPI cannot install the two
things a first connection installs. See `DENY_PRIORITY` for the whole of that
argument.

Standalone-loadable on purpose, like connect.py beside it: stdlib only, nothing
from `plexora` at module level. One seam -- `_RUNNER` -- is where every
subprocess goes, so tests drive the whole of this on canned JSON without a
Google account.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time


class GcloudError(RuntimeError):
    """A gcloud call that failed, worded for the person who has to fix it.

    Carries `detail` -- the tail of stderr -- separately from the sentence, so
    a caller can show the sentence and keep the raw text for the log. Google's
    own errors are usually the actionable half (a disabled API names the API
    and the console URL to enable it), so they are quoted rather than
    swallowed.

    `recovery` names a fix the user can press rather than read: a short key
    from `RECOVERY_*` that a surface turns into one button. It exists because
    a sentence ending "ask for a Standard one instead" describes a change to
    the saved profile that the reader would otherwise have to go and make by
    hand, on a form three pages deep, having memorised which page. Empty for
    every failure whose fix is not a single unambiguous edit -- which is most
    of them, and the reason this is a named key rather than free text a
    browser tries to interpret.
    """

    def __init__(self, message, detail="", recovery=""):
        super().__init__(message if not detail else f"{message}\n{detail}")
        self.message = message
        self.detail = detail
        self.recovery = str(recovery or "")


# -- the one subprocess seam ------------------------------------------------

#: How long a read-only lookup may take. Long enough for a cold `gcloud` (it
#: loads a Python interpreter of its own) on a laptop that has not run it
#: today, short enough that a form waiting on it is not thought to be broken.
QUERY_TIMEOUT = 90
#: Creating a VM. Blocking, because the ladder afterwards has nothing to do
#: until the instance exists, and an async create would only move the wait.
CREATE_TIMEOUT = 420
#: Starting a VM that already exists -- an image that is already built, so
#: much shorter than a create.
START_TIMEOUT = 240
#: One `gcloud compute ssh --command true`, used to find out whether the VM is
#: answering yet. Short: it is retried, and a hung probe is the failure mode
#: worth cutting off early.
SSH_PROBE_TIMEOUT = 75
#: How long to keep probing before calling a new VM dead. A fresh boot plus
#: OS Login key propagation is usually well under a minute; five is the point
#: past which something is actually wrong.
SSH_READY_TIMEOUT = 420
#: How long to then wait for the first-boot startup script to be FINISHED --
#: which is a different question from whether sshd is answering, and the one
#: that actually decides whether this machine can be given work.
#:
#: sshd answers within seconds of boot, long before the startup script has run
#: `apt-get install`. On a big machine the difference does not show. On the
#: smallest tier this preset offers -- an e2-medium is 4 GB of RAM and a
#: *fraction* of two vCPUs -- an apt install is most of what the machine has,
#: and the session's own ssh arriving in the middle of it is a connection made
#: to a host with nothing left to answer with.
#:
#: Ten minutes because the thing being waited for is an apt install on the
#: slowest hardware on the menu, and because overrunning it is not fatal: the
#: mount chain installs gcsfuse itself if it has to.
STARTUP_READY_TIMEOUT = 600


def _default_runner(argv, timeout=None):
    """Run one gcloud, return `(returncode, stdout, stderr)`.

    Captured rather than streamed: everything routed through here is a
    request/response with a JSON answer. The one thing that genuinely streams
    -- the ssh that carries the session -- is not run from this module at all;
    it is spawned by `connect._Watched` so its output reaches the log.
    """
    done = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout)
    return done.returncode, done.stdout or "", done.stderr or ""


#: The seam. Rebound by tests on the loaded module object, so the whole of this
#: file is exercised against canned JSON and recorded argv with no Google
#: account, no network and no gcloud installed.
_RUNNER = _default_runner
_which = shutil.which
_now = time.monotonic
_sleep = time.sleep


def available():
    """Whether the gcloud CLI is installed on THIS machine."""
    return bool(_which("gcloud"))


def _run(args, timeout=QUERY_TIMEOUT):
    """`gcloud <args>`, with no interpretation of the result."""
    argv = ["gcloud", *[str(part) for part in args]]
    try:
        return _RUNNER(argv, timeout=timeout)
    except FileNotFoundError:
        raise GcloudError(
            "The Google Cloud CLI (`gcloud`) is not installed on this "
            "machine. Install it from cloud.google.com/sdk and sign in with "
            "`gcloud auth login`.") from None
    except subprocess.TimeoutExpired:
        raise GcloudError(
            f"`gcloud {' '.join(str(part) for part in args[:3])}…` did not "
            f"answer within {timeout:g}s.") from None


def _tail(text, count=6):
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def run_json(args, timeout=QUERY_TIMEOUT, what=""):
    """One gcloud query, as the object it printed.

    `--quiet` because nothing here is allowed to stop and ask: this runs in a
    request thread, or inside a connection, and a gcloud prompting on a
    console nobody is watching is a hang rather than a question.
    """
    code, out, err = _run([*args, "--format=json", "--quiet"], timeout=timeout)
    if code != 0:
        raise GcloudError(
            what or "Google Cloud refused that request.", _tail(err))
    try:
        return json.loads(out or "null")
    except ValueError:
        raise GcloudError(
            what or "Google Cloud answered with something that is not JSON.",
            _tail(out)) from None


# -- who is signed in -------------------------------------------------------


def account():
    """The signed-in Google account, or None.

    None is not an error and must not be reported as one: "you are not signed
    in yet" is the ordinary first state of the form, and its answer is a
    button, not a stack trace.
    """
    if not available():
        return None
    try:
        listed = run_json(["auth", "list", "--filter=status:ACTIVE"],
                          what="Could not read your gcloud sign-in state.")
    except GcloudError:
        return None
    for entry in listed or ():
        name = (entry or {}).get("account")
        if name:
            return str(name)
    return None


def begin_login():
    """Start `gcloud auth login` and return immediately.

    Detached on purpose. The browser flow is a person reading a consent screen,
    which is not a thing a request may block on -- so this only starts it, and
    the form polls `account()` until a name appears. gcloud writes the
    credential into its own store; nothing comes back through this process, and
    there is nothing here to record.
    """
    if not available():
        raise GcloudError(
            "The Google Cloud CLI (`gcloud`) is not installed on this "
            "machine. Install it from cloud.google.com/sdk first.")
    try:
        subprocess.Popen(["gcloud", "auth", "login", "--brief"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise GcloudError("Could not start `gcloud auth login`.",
                          str(exc)) from None
    return True


# -- what you have ----------------------------------------------------------


def projects():
    """Every project this account can use, newest-looking name first."""
    listed = run_json(["projects", "list"],
                      what="Could not list your Google Cloud projects.")
    out = []
    for entry in listed or ():
        project_id = (entry or {}).get("projectId")
        if not project_id:
            continue
        out.append({"id": str(project_id),
                    "name": str(entry.get("name") or project_id)})
    return sorted(out, key=lambda item: item["id"])


def buckets(project):
    """Every Cloud Storage bucket in `project`, with where each one lives.

    The location rides along because it is the answer to the question the form
    asks next: compute belongs in the region the data is already in, and a
    dropdown that made somebody look each bucket up in the console would be
    asking them to do the join by hand.
    """
    listed = run_json(["storage", "buckets", "list", "--project", project],
                      what=f"Could not list the buckets in “{project}”.")
    out = []
    for entry in listed or ():
        name = (entry or {}).get("name")
        if not name:
            continue
        out.append(_bucket_view(entry))
    return sorted(out, key=lambda item: item["name"])


def _bucket_view(entry, public=False):
    location = str((entry or {}).get("location") or "").strip()
    region, exact = region_for_bucket_location(location)
    return {
        "name": str(entry.get("name")),
        "location": location,
        "location_type": str(entry.get("location_type")
                             or entry.get("locationType") or ""),
        "region": region,
        # Whether that region IS the bucket's location or merely the nearest
        # sensible one. A multi-region bucket has no single region to match,
        # and the form says which of the two it is looking at.
        "exact": exact and not public,
        # Readable, but not by this account's own rights -- see `bucket()`.
        # Carried through so the form can say why it could not fill the region
        # in, instead of leaving a field that filled itself everywhere else
        # mysteriously blank on one bucket.
        "public": bool(public),
    }


#: What a bucket name may contain. Checked here rather than left to Google
#: because this name is spliced into a shell command line on the VM (gcsfuse
#: takes it as an argument), and a name that cannot contain a metacharacter
#: cannot carry one there.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


def valid_bucket_name(name):
    return bool(_BUCKET_RE.match(str(name or "").strip()))


def bucket(project, name):
    """One bucket, checked. Raises with a plain sentence when it is not usable.

    The whole of the "does this bucket exist and can you read it" question,
    answered before anything is provisioned -- a mount that fails after a VM
    has been created is the same mistake discovered four minutes and some
    money later.

    Two questions, in fact, and the second one only when the first is refused.
    Describing a bucket needs `storage.buckets.get`, which is a permission on
    the bucket's own metadata -- and **a public bucket does not grant it.**
    Somebody else's published atlas is world-READABLE: `allUsers` gets Storage
    Object Viewer, which is objects, not metadata. So a describe that comes
    back 403 is not proof the bucket is unusable; it is a reason to ask the
    question the mount will actually ask, which is whether the objects can be
    listed. See `_readable_anyway`.
    """
    text = str(name or "").strip()
    if not valid_bucket_name(text):
        raise GcloudError(
            f"“{text}” is not a Cloud Storage bucket name. Bucket names are "
            "lower-case letters, digits, dashes, underscores and dots.")
    try:
        described = run_json(
            ["storage", "buckets", "describe", f"gs://{text}",
             "--project", project],
            what=f"Could not read the bucket gs://{text}.")
    except GcloudError as exc:
        detail = (exc.detail or "").lower()
        if "not found" in detail or "404" in detail:
            raise GcloudError(
                f"There is no bucket called gs://{text} in “{project}”. "
                "Check the name, or pick one from the list.") from None
        if "403" in detail or "permission" in detail or "denied" in detail:
            if _readable_anyway(project, text):
                return _bucket_view({"name": text}, public=True)
            raise GcloudError(
                f"This account cannot read gs://{text}. Ask whoever owns the "
                "bucket for Storage Object Viewer on it (Storage Object User "
                "if Plexora should be able to write).") from None
        raise
    if not described:
        raise GcloudError(f"There is no bucket called gs://{text} in "
                          f"“{project}”.")
    if isinstance(described, list):
        described = described[0]
    return _bucket_view(described)


def _readable_anyway(project, name):
    """Can the OBJECTS be listed, whatever the metadata said?

    The fallback behind `bucket()`, and the reason a published dataset can be
    named on this form at all. One object is asked for, not a listing: the
    question is whether the read is permitted, and a bucket with four million
    objects in it should answer that as fast as a bucket with one.

    Any failure is False. This is the second of two chances, and a maybe here
    would be worse than a no -- the sentence it suppresses is the one that
    tells somebody exactly which permission to ask for.
    """
    try:
        code, _out, _err = _run(
            ["storage", "objects", "list", f"gs://{name}",
             "--project", project, "--limit=1", "--page-size=1",
             "--format=json", "--quiet"])
    except GcloudError:
        return False
    return code == 0


# -- where a bucket is, as a place to put a VM ------------------------------

#: Where to compute on data that is in a multi-region or a dual-region. There
#: is no right answer -- the data is genuinely in several places -- so these
#: are the region each multi-region's own documentation treats as its centre
#: of gravity. Marked inexact wherever they are used, because a guess
#: presented as a match is how somebody ends up paying egress they were told
#: they had avoided.
_MULTI_REGION = {
    "US": "us-central1",
    "EU": "europe-west1",
    "ASIA": "asia-east1",
    # The predefined dual-regions, resolved to one of their own two members.
    "NAM4": "us-central1",
    "EUR4": "europe-west4",
    "ASIA1": "asia-northeast1",
}

#: A GCP region: `us-east1`, `europe-west4`, `australia-southeast2`. Written
#: out because the AWS spelling (`us-east-1`) is close enough to be typed by
#: mistake and far enough to match nothing.
_REGION_RE = re.compile(r"^[a-z]+(?:-[a-z]+)+\d+$")

#: A zone is a region plus a letter.
_ZONE_RE = re.compile(r"^[a-z]+(?:-[a-z]+)+\d+-[a-z]$")

DEFAULT_REGION = "us-east1"


def region_for_bucket_location(location):
    """`(region, exact)` for a bucket's location.

    `exact` is the whole point: a bucket in `US-EAST1` has a region, and
    putting the VM anywhere else is a mistake worth warning about; a bucket in
    `US` does not, and warning about it would be warning about nothing.
    """
    text = str(location or "").strip()
    if not text:
        return DEFAULT_REGION, False
    lowered = text.lower()
    if _REGION_RE.match(lowered):
        return lowered, True
    mapped = _MULTI_REGION.get(text.upper())
    if mapped:
        return mapped, False
    return DEFAULT_REGION, False


def valid_region(region):
    return bool(_REGION_RE.match(str(region or "").strip()))


def valid_zone(zone):
    return bool(_ZONE_RE.match(str(zone or "").strip()))


def region_of_zone(zone):
    text = str(zone or "").strip()
    return text.rsplit("-", 1)[0] if valid_zone(text) else ""


#: The regions the form offers, with what to call them. Not fetched: a
#: dropdown that costs a network round trip before it can be drawn is a
#: dropdown somebody waits for, and the answer is nearly always "the one the
#: bucket is in" anyway -- which is filled in for them. Anything not here can
#: still be typed into Advanced, and `valid_region` is what actually decides.
REGIONS = (
    ("us-east1", "us-east1 · South Carolina"),
    ("us-east4", "us-east4 · Northern Virginia"),
    ("us-central1", "us-central1 · Iowa"),
    ("us-west1", "us-west1 · Oregon"),
    ("us-west2", "us-west2 · Los Angeles"),
    ("northamerica-northeast1", "northamerica-northeast1 · Montréal"),
    ("southamerica-east1", "southamerica-east1 · São Paulo"),
    ("europe-west1", "europe-west1 · Belgium"),
    ("europe-west2", "europe-west2 · London"),
    ("europe-west3", "europe-west3 · Frankfurt"),
    ("europe-west4", "europe-west4 · Netherlands"),
    ("europe-north1", "europe-north1 · Finland"),
    ("asia-east1", "asia-east1 · Taiwan"),
    ("asia-northeast1", "asia-northeast1 · Tokyo"),
    ("asia-south1", "asia-south1 · Mumbai"),
    ("asia-southeast1", "asia-southeast1 · Singapore"),
    ("australia-southeast1", "australia-southeast1 · Sydney"),
)


def zones(project, region):
    """The zones of `region` that are accepting work right now."""
    listed = run_json(
        ["compute", "zones", "list", "--project", project,
         f"--filter=region:( {region} )"],
        what=f"Could not list the zones in {region}.")
    out = []
    for entry in listed or ():
        name = (entry or {}).get("name")
        if not name:
            continue
        if str(entry.get("status") or "UP").upper() != "UP":
            continue
        out.append(str(name))
    return sorted(out)


def pick_zone(project, region):
    """One zone in `region`, or "" when nothing there is up.

    Any zone will do -- the VM and the bucket are matched by REGION, which is
    what egress and latency are charged by -- so the first one that is up is
    the answer, and offering the choice at all is an Advanced-box courtesy.
    """
    try:
        found = zones(project, region)
    except GcloudError:
        return ""
    return found[0] if found else ""


# -- what to ask for --------------------------------------------------------

#: The machine types the form offers. Curated rather than fetched for the same
#: reason `REGIONS` is, plus one more: `gcloud compute machine-types list`
#: returns hundreds of rows per zone, most of which are wrong for this work,
#: and a picker with everything in it is a picker nobody can choose from.
#:
#: Eight rows, in three groups, because there are three different reasons to be
#: here. One small type for trying the connection out -- somebody checking that
#: the bucket mounts and the tunnel opens should not have to rent 128 GB of RAM
#: to find out. Four general-purpose. Three memory-heavy, for the actual work: a
#: 40-channel pyramid is tens of gigabytes before anything is drawn, which is
#: why the DEFAULT is the same shape as the cluster presets' `--mem 128G`
#: rather than the cheapest thing that boots.
#:
#: **Shorter than it was, on purpose.** It used to carry sixteen rows including
#: e2-micro and e2-small, and a picker whose first two entries are 1 GB and 2 GB
#: of RAM is a picker offering a choice nobody working on imaging data should
#: make. A shortlist that has to be read to the end is not much better than the
#: catalogue it was standing in for.
#:
#: Still not exhaustive, and deliberately so -- the form offers a box for a
#: type this list does not name, which is the escape hatch for GPUs, C3, and
#: `custom-4-8192`. See `valid_machine_type`.
MACHINE_TYPES = (
    ("e2-medium", 2, 4),
    ("e2-standard-4", 4, 16),
    ("e2-standard-8", 8, 32),
    ("e2-standard-16", 16, 64),
    ("e2-highmem-4", 4, 32),
    ("e2-highmem-8", 8, 64),
    ("e2-highmem-16", 16, 128),
    ("n2-highmem-32", 32, 256),
)

#: The types whose vCPU number is a burst ceiling rather than whole cores. Said
#: out loud in the label because "2 vCPU" on an e2-medium and "2 vCPU" on a
#: dedicated-core type are not the same offer, and nothing else in the name
#: would tell them apart.
SHARED_CORE = frozenset({"e2-medium"})

DEFAULT_MACHINE_TYPE = "e2-highmem-16"

#: Compute Engine's own shape for a machine type name, which also covers the
#: `custom-4-8192` and `n2-custom-8-16384` forms. Checked rather than matched
#: against `MACHINE_TYPES`, because the whole point of letting somebody type
#: one is that the curated list does not have to be complete -- but a typo
#: should be caught here, in a sentence, rather than by `instances create`
#: four screens later.
_MACHINE_TYPE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def valid_machine_type(name):
    text = str(name or "")
    return bool(_MACHINE_TYPE_RE.match(text)) and len(text) <= 60


#: How the VM is bought. Spot is the same hardware at a 60-91% discount, on the
#: condition that Compute Engine may take it back with thirty seconds' notice
#: when somebody paying full price wants the capacity.
#:
#: **Spot is the default, and the reason is what preemption actually costs
#: HERE.** For a long-running server it is a serious risk; for this preset it is
#: an interruption. The data is in the bucket, not on the machine, so nothing is
#: lost that was not lost by closing the laptop -- and `--instance-termination-
#: action=STOP` means a preempted VM is *stopped* rather than deleted, so the
#: disk with `~/plexora-venv` on it survives and the reuse ladder starts the
#: same machine again on the next connection. What preemption costs is a
#: reconnect. What Standard costs is three to ten times the hourly rate, every
#: hour, whether or not anything was ever going to preempt it.
#:
#: Standard is there for the session somebody cannot afford to have interrupted
#: -- a long unattended import, a demo -- and the form says so in one sentence.
PROVISIONING_SPOT = "spot"
PROVISIONING_STANDARD = "standard"
DEFAULT_PROVISIONING = PROVISIONING_SPOT

#: The one failure this module can offer a button for instead of a paragraph.
#:
#: A zone with no spare Spot capacity is not a broken configuration and not a
#: mistake -- it is a queue somebody else is at the front of, and the fix is a
#: single field on the saved profile. Naming it here, rather than leaving the
#: browser to recognise the sentence, keeps the recovery attached to the
#: failure that warrants it: the same words about capacity mean something else
#: entirely on a Standard request, where the answer really is another zone.
RECOVERY_STANDARD = "standard"


def valid_provisioning(name):
    return str(name or "") in (PROVISIONING_SPOT, PROVISIONING_STANDARD)


def provisioning_models():
    """The two ways to buy the machine, as the form draws them."""
    return [
        {"name": PROVISIONING_SPOT, "label": "Spot (preemptible)",
         "hint": "Much cheaper — usually 60–91% off. Google can reclaim the "
                 "machine at any time; Plexora asks for it to be stopped "
                 "rather than deleted, so your environment survives and "
                 "reconnecting starts it again."},
        {"name": PROVISIONING_STANDARD, "label": "Standard",
         "hint": "Full price, and nobody takes it away. Worth it for a long "
                 "import you are not watching, or anything you cannot have "
                 "interrupted."},
    ]


#: What happens to the machine when the session ends. Three answers, and they
#: are genuinely different decisions rather than degrees of one: keep paying for
#: compute, keep paying for the disk, or keep paying for nothing.
#:
#: `EXIT_STOP` is the default because it is the one that is wrong in the
#: cheapest direction. Leaving a 16-core machine running is a bill that grows
#: while nobody is looking; deleting one costs a rebuild that somebody notices
#: immediately and can undo by connecting again.
EXIT_LEAVE = "leave"
EXIT_STOP = "stop"
EXIT_DELETE = "delete"
DEFAULT_EXIT = EXIT_STOP


def valid_exit(name):
    return str(name or "") in (EXIT_LEAVE, EXIT_STOP, EXIT_DELETE)


def exit_actions():
    """The three endings, with what each one costs, as the form draws them."""
    return [
        {"name": EXIT_LEAVE, "label": "Leave VM running",
         "hint": "The VM stays up and reconnecting is instant. Compute keeps "
                 "billing the whole time, including overnight."},
        {"name": EXIT_STOP, "label": "Stop VM",
         "hint": "Compute stops billing. The disk keeps its charge — about $2 "
                 "a month at 20 GB — and everything Plexora installed is still "
                 "on it, so the next connection takes under a minute."},
        {"name": EXIT_DELETE, "label": "Delete VM",
         "hint": "The VM and its disk are removed, so nothing keeps billing. "
                 "The next connection builds a new machine, which takes a few "
                 "minutes. Your bucket and everything in it are untouched — "
                 "Plexora never deletes storage."},
    ]


def exit_action(record):
    """What to do with this profile's machine when the session ends.

    Reads the saved record rather than being told, because four separate
    teardown paths ask this question -- the Disconnect button, a session thread
    ending, a failed connect, and the app quitting -- and the one thing that
    must not vary between them is the answer.

    Two rules are applied here rather than trusted to whoever saved the record:

    - **A machine the user already runs is never deleted, whatever the record
      says.** The saved field can be edited, imported or simply wrong;
      `delete_instance` makes the same refusal against the label on the
      instance itself, and this one exists so the question is never even
      asked. Stopping one IS allowed -- that is a person choosing, on the form,
      what should happen to their own machine -- but deleting is not something
      Plexora offers to do to a thing it did not make.
    - **A record from before this field existed still means something.** v3 and
      earlier stored a `stop_vm_on_disconnect` boolean, which is exactly the
      two-valued version of this question, and reading it as such is what stops
      every profile saved last week from silently switching to "leave running".
    """
    record = record or {}
    named = str(record.get("on_exit") or "")
    if not valid_exit(named):
        legacy = record.get("stop_vm_on_disconnect")
        if legacy is None:
            named = DEFAULT_EXIT
        else:
            named = EXIT_STOP if legacy else EXIT_LEAVE
    if named == EXIT_DELETE and record.get("vm_source") == VM_EXISTING:
        return EXIT_LEAVE
    return named

#: Where the bucket is mounted on the VM, and therefore Plexora's data
#: directory for the session. Under the user's own home rather than /mnt: a
#: gcsfuse mount there needs no sudo, which means the mount step cannot fail
#: on a VM whose OS Login user has no sudo rights.
DEFAULT_MOUNT_PATH = "~/plexora-data"

#: Boot disk. **The images are not on it and neither is the project** -- the
#: data is in the bucket, and a `kind=node` session's databases are on the
#: user's own machine. What is on it is four things:
#:
#:   * the Debian image, about 2 GB used;
#:   * `~/plexora-venv` -- around 1.5 GB across some thirty thousand files,
#:     because Plexora's stack is scipy, scikit-image, scikit-learn, polars,
#:     pyarrow, spatialdata, imagecodecs and the rest of it;
#:   * pip's download cache while that installs, roughly another gigabyte;
#:   * **gcsfuse's staging area.** This is the one that is not a constant:
#:     writing an object to the bucket stages the whole of it locally first,
#:     so the disk has to be bigger than the largest single file a session
#:     will write. See `GCSFUSE_TEMP`.
#:
#: So the capacity wanted is about 5 GB plus room to write, and the disk is the
#: one thing that goes on billing after the VM stops -- forever, until somebody
#: deletes it. It was 200 GB, then 50; 20 is what the default is now.
#:
#: What that trades away is speed, and it is worth knowing which way: on
#: pd-balanced **throughput and IOPS are sold by the gigabyte** -- 6 IOPS and
#: 0.28 MB/s per GB -- so 20 GB is 120 IOPS and 5.6 MB/s, and the first
#: connection's job is unpacking thirty thousand files into a venv, which is
#: precisely an IOPS-bound workload. The first connection to a new VM is
#: therefore slower on this default than it was on 50 GB, and every connection
#: after it is unaffected, because the venv is already there.
#:
#: The floor is Google's own: a boot disk may not be smaller than the image it
#: is built from, and the Debian cloud image is 10 GB. Plexora used to impose
#: 30 on top of that, which is no longer defensible now that the default is
#: below it.
DEFAULT_BOOT_DISK_GB = 20
MIN_BOOT_DISK_GB = 10

#: Where gcsfuse stages an object it is writing. Named rather than left to the
#: default of `/tmp`, for two reasons: on an image where `/tmp` is a tmpfs
#: this would be staging into RAM, and a large write would end as an OOM kill
#: rather than as a disk that filled; and a staging area whose location is
#: unspecified cannot be reasoned about when choosing a disk size, which is
#: the whole of the paragraph above.
GCSFUSE_TEMP = "~/.plexora-gcsfuse-tmp"

#: The image a rented VM is built from. **Debian 13, not 12, and the reason is
#: Python.** Plexora's own `requires-python` is `>=3.12,<3.14`; Debian 12
#: (bookworm) ships Python 3.11, so `pip install plexora` on one ends in
#: "No matching distribution found for plexora" after everything else about
#: the connection has worked perfectly. Trixie ships 3.13.
#:
#: Changing this changes which gcsfuse apt suite is right, which is why
#: neither the startup script nor `_GCSFUSE_INSTALL` names one any more --
#: both read `VERSION_CODENAME` from `/etc/os-release` on the machine itself.
IMAGE_FAMILY = "debian-13"
IMAGE_PROJECT = "debian-cloud"

#: What `pip install plexora` will accept, kept in step with pyproject.toml by
#: a test rather than by anybody remembering. Written here because this module
#: may not import `plexora` -- see the standalone-loadable rule at the top.
MIN_PYTHON = (3, 12)

#: Image families Plexora itself used to default to, and which a saved profile
#: must not be held to.
#:
#: Nothing in the form can set `image_family` -- it is not a question anybody
#: is asked -- so a profile naming one is not expressing a preference. It is
#: carrying the default from the version of Plexora that wrote it. Honouring
#: that would mean every connection saved before this change kept building
#: Debian 12 VMs, and kept failing on Python 3.11, no matter how many times
#: somebody deleted the machine and let it be rebuilt.
SUPERSEDED_IMAGE_FAMILIES = frozenset({"debian-12"})


def image_family(cfg):
    """Which image to build from: the profile's, unless it is a stale default."""
    named = str(_cfg(cfg, "image_family") or "").strip()
    if not named or named in SUPERSEDED_IMAGE_FAMILIES:
        return IMAGE_FAMILY
    return named

#: Where Google's IAP TCP forwarding connects from -- the only source that
#: ever needs to reach port 22 on a Plexora VM, whether or not the machine has
#: a public address of its own.
IAP_RANGE = "35.235.240.0/20"
FIREWALL_RULE = "plexora-allow-iap-ssh"

#: The network tag every VM Plexora creates carries, and the thing the ingress
#: rules below are scoped to. Tag-scoped rather than network-wide on purpose:
#: `DENY_RULE` shuts a machine off from the internet, and a rule that could do
#: that to something Plexora did not create would be a tool reaching outside
#: its own work. An untagged VM in the same project is untouched by both.
NET_TAG = "plexora"
DENY_RULE = "plexora-deny-public-ingress"

#: Why the deny rule exists at all, and why its priority is what it is.
#:
#: A VM needs a route OUT -- to Google's apt repository for gcsfuse, and to
#: PyPI for Plexora itself, which is not a Google domain and so is not reached
#: by Private Google Access either. The two ways to have one are Cloud NAT
#: (~$32 a month for the gateway, before traffic) and a public IP address on
#: the instance (free, and only while the VM runs). For a preset whose default
#: machine is billed by the hour and stopped between sessions, a gateway that
#: bills every hour of every month is not a default anybody would want.
#:
#: So the VM gets an address, and this rule takes back what the address gives
#: away: deny every ingress to tagged machines except the IAP range that
#: `FIREWALL_RULE` allows. Egress is untouched, which is the whole point.
#:
#: 65000 is chosen to sit just under the default VPC's own `default-allow-ssh`
#: and friends at 65534 -- so it overrides the permissive rules a project
#: arrives with -- and far above the 1000 an administrator's deliberate rule
#: gets by default, so it never overrides a decision somebody actually made.
DENY_PRIORITY = 65000

#: Written by the startup script when it has finished, whatever the outcome,
#: and read by the mount chain. Its *contents* are the outcome: waiting for
#: the file means a first connection stops waiting the moment there is nothing
#: left to wait for, instead of sitting out the full five minutes to discover
#: that an install failed in the first thirty seconds.
#:
#: Under `/var/run`, which is a tmpfs and therefore cleared on every boot --
#: and that is right rather than incidental. Compute Engine runs the startup
#: script on every boot too, so the marker's lifetime and the script's are the
#: same one. A VM whose first boot had no route to the internet has no stale
#: "done" left over to skip the wait on the boot that does.
STARTUP_MARK = "/var/run/plexora-startup-done"

#: Where the first-boot script keeps a copy of everything it said, and where
#: the mount chain's own install attempt keeps its output.
#:
#: On disk rather than left to the serial console, which sounds like the
#: obvious place and is not: Compute Engine only serves
#: `get-serial-port-output` for a **running** instance. A VM that failed to
#: install something is a VM Plexora then stops -- so by the time anybody
#: goes looking, the one place the reason was written has stopped answering.
#: These two files survive the stop and are read back by the connection that
#: hits the failure, which is the only moment anybody actually wants them.
STARTUP_LOG = "/var/log/plexora-startup.log"
INSTALL_LOG = "/tmp/plexora-gcsfuse-install.log"

#: Whose machine this is. The whole of the ownership rule lives in this pair:
#: `plexora` may be created, stopped and deleted by us, `existing` may be
#: none of those three.
VM_PLEXORA = "plexora"
VM_EXISTING = "existing"

#: The label a VM Plexora created carries, and the *only* thing
#: `delete_instance` will accept as proof that deleting is allowed. Written at
#: create; checked, not assumed, at delete.
OWNER_LABEL = "created-by"
OWNER_VALUE = "plexora"

#: How long a rented VM may sit with nobody logged in before it shuts itself
#: down. The last line of defence against a bill: every host-side teardown
#: path can be skipped by a laptop that dies, a power cut or a SIGKILL, and
#: none of them can be made to survive that. A timer on the VM survives all of
#: it, because the only thing it depends on is the VM being up.
#:
#: Thirty minutes because the thing it measures is "no ssh session at all",
#: not "nobody typing": Plexora holds its ssh open for the life of a
#: connection, so a long analysis with the browser closed still counts as
#: busy. The window only starts when the last session has genuinely gone.
IDLE_SHUTDOWN_MINUTES = 30
MIN_IDLE_SHUTDOWN_MINUTES = 5


def machine_type_label(name):
    """`e2-highmem-16 · 16 vCPU · 128 GB RAM` -- the size, said in sizes.

    A machine type name encodes the answer and does not say it. Somebody
    choosing between `e2-highmem-16` and `n2-highmem-32` is choosing between
    128 GB and 256 GB of RAM, and the form should be the place they find that
    out rather than a documentation page.
    """
    for entry, cpus, memory in MACHINE_TYPES:
        if entry == name:
            cores = "shared vCPU" if entry in SHARED_CORE else "vCPU"
            return f"{entry} · {cpus} {cores} · {memory} GB RAM"
    return str(name or "")


def machine_types():
    """The catalogue, as the form draws it."""
    return [{"name": name, "label": machine_type_label(name),
             "cpus": cpus, "memory_gb": memory,
             "shared": name in SHARED_CORE}
            for name, cpus, memory in MACHINE_TYPES]


def regions():
    return [{"name": name, "label": label} for name, label in REGIONS]


#: What a Plexora VM is called, derived from the profile's name so that
#: reconnecting finds the same machine without an instance id being stored
#: anywhere. Lower-case, dashes, starts with a letter: Compute Engine's own
#: rule, and the reason the profile name is slugged rather than used as typed.
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def instance_name(profile_name):
    slug = _SLUG_RE.sub("-", str(profile_name or "").strip().lower())
    slug = slug.strip("-") or "vm"
    return f"plexora-{slug}"[:62]


#: Compute Engine's own rule for an instance name, which is also what makes it
#: safe to put one on a `gcloud` command line: lower-case, digits and dashes,
#: starting with a letter. Checked rather than assumed for a name somebody
#: typed, because a name that reaches `gcloud compute ssh` is a name that
#: reaches a subprocess.
_INSTANCE_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}$")


def valid_instance_name(name):
    text = str(name or "")
    return bool(_INSTANCE_RE.match(text)) and not text.endswith("-")


# -- the VM -----------------------------------------------------------------


def _cfg(cfg, key, default=""):
    value = (cfg or {}).get(key, default)
    return value if value is not None else default


def instance(project, zone, name):
    """What is true about one VM, or None when it does not exist.

    Not-found is a return value rather than an exception because it is the
    ordinary first answer: a profile that has never been connected names a VM
    nobody has created yet, and that is the branch that creates it.

    Four facts, each load-bearing somewhere: `status` drives the reuse ladder,
    `labels` is how `delete_instance` knows whose machine this is, `scopes`
    lets the mount step say "this VM cannot read Cloud Storage" before gcsfuse
    fails with a 403, and `external_ip` is how a VM the user already runs can
    be reached when IAP is not set up on their network.
    """
    code, out, err = _run(["compute", "instances", "describe", name,
                           "--project", project, "--zone", zone,
                           "--format=json", "--quiet"])
    if code != 0:
        lowered = (err or "").lower()
        if "was not found" in lowered or "not found" in lowered:
            return None
        raise GcloudError(
            f"Could not check whether the VM “{name}” exists.", _tail(err))
    try:
        described = json.loads(out or "null")
    except ValueError:
        return None
    if not described:
        return None
    scopes = []
    for entry in described.get("serviceAccounts") or ():
        if isinstance(entry, dict):
            scopes += [str(one) for one in entry.get("scopes") or ()]
    external = ""
    for nic in described.get("networkInterfaces") or ():
        for access in (nic or {}).get("accessConfigs") or ():
            if isinstance(access, dict) and access.get("natIP"):
                external = str(access["natIP"])
                break
    labels = described.get("labels")
    tags = (described.get("tags") or {}).get("items") or ()
    return {"status": str(described.get("status") or "").upper(),
            "machine_type": str(described.get("machineType") or "")
            .rsplit("/", 1)[-1],
            "labels": dict(labels) if isinstance(labels, dict) else {},
            "scopes": scopes,
            # Read so that a VM created before `DENY_RULE` existed can be
            # brought under it rather than quietly left outside it. See
            # `repair_egress`.
            "tags": [str(tag) for tag in tags],
            "external_ip": external}


def instances(project, zone=""):
    """Every VM in a project, or in one zone of it, for the BYO picker.

    Each row carries the zone because the form needs it: an instance name is
    only unique within a zone, and a picker that returned bare names would
    make somebody choose between two machines it had drawn identically.
    """
    argv = ["compute", "instances", "list", "--project", project]
    if zone:
        argv += ["--zones", zone]
    described = run_json(argv, what="Could not list the VMs in this project.")
    out = []
    for entry in described or ():
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        out.append({
            "name": str(entry["name"]),
            "zone": str(entry.get("zone") or "").rsplit("/", 1)[-1],
            "status": str(entry.get("status") or "").upper(),
            "machine_type": str(entry.get("machineType") or "")
            .rsplit("/", 1)[-1],
        })
    out.sort(key=lambda one: (one["zone"], one["name"]))
    return out


def zone_of_instance(project, name):
    """Which zone one VM is in, found by name across the whole project.

    An instance name is only unique within a zone, so this returns the first
    match and callers with two machines of the same name in different zones
    have to say which. That is rare enough to be worth the convenience of not
    making everybody else name a zone: somebody who knows their VM is called
    `analysis-box` should not have to remember where they put it.
    """
    for entry in instances(project):
        if entry.get("name") == name:
            return entry.get("zone") or ""
    return ""


def made_by_plexora(described):
    """Did Plexora create this VM? The label is the only accepted answer.

    Not the profile's own `vm_source` -- that is a saved field, and a saved
    field can be edited, imported or simply wrong. The label was written by
    `create_instance` on the machine itself, so it is the one claim that
    travels with the thing being deleted.
    """
    labels = (described or {}).get("labels") or {}
    return str(labels.get(OWNER_LABEL) or "") == OWNER_VALUE


#: The scopes that let gcsfuse authenticate as the instance. `cloud-platform`
#: is the superset Google's own console offers as "allow full access".
_STORAGE_SCOPES = (
    "https://www.googleapis.com/auth/devstorage.read_write",
    "https://www.googleapis.com/auth/devstorage.full_control",
    "https://www.googleapis.com/auth/cloud-platform",
)


def can_reach_storage(described):
    """Whether this VM's scopes permit writing to Cloud Storage at all.

    Scope is not permission -- the bucket's IAM decides that, and the mount
    step checks it for real -- but a VM created with `storage-ro`, or with the
    default scopes and no storage in them, cannot even try. Worth saying
    before the mount rather than after, because the fix is on the instance
    (a stop, an edit and a start) rather than on the bucket.
    """
    scopes = (described or {}).get("scopes") or ()
    if not scopes:
        return True     # nothing described; let the mount be the judge
    return any(one in _STORAGE_SCOPES for one in scopes)


def wants_external_ip(cfg):
    """Whether this profile's VM should have an address of its own.

    Absent means yes, and that is the important half. Every profile written
    before this field existed describes a VM with `--no-address` on a subnet
    that was never checked for a way out -- which is exactly the machine that
    cannot install anything. Reading a missing key as "yes" is what lets one
    of those repair itself on the next connection instead of failing the same
    way forever. See `repair_egress`.
    """
    value = _cfg(cfg, "external_ip", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def repair_egress(cfg, described, echo=print):
    """Give a VM that already exists the tag and the address it now needs.

    A VM created by an earlier Plexora has neither, and reconnecting to it
    would reuse a machine that still cannot reach Google's apt repository --
    the same failure, forever, on a machine that looks perfectly healthy from
    the outside.

    **Tag first, address second, and never the other way round.** The tag is
    what `DENY_RULE` matches on, so a machine that got its address first would
    spend the seconds in between answering the whole internet. Both steps work
    on a running instance, so this costs a reconnect nothing.

    Only for rented VMs. Adding a public address to a machine somebody else
    runs is not a repair, it is a change to their server.
    """
    project = _cfg(cfg, "project")
    zone = _cfg(cfg, "zone")
    name = _cfg(cfg, "vm_name")
    if NET_TAG not in ((described or {}).get("tags") or ()):
        code, _out, err = _run(
            ["compute", "instances", "add-tags", name, "--project", project,
             "--zone", zone, f"--tags={NET_TAG}", "--quiet"])
        if code != 0:
            echo("  Could not tag the VM, so Plexora will not give it a "
                 "public address either — the two go together.")
            return False
    if (described or {}).get("external_ip"):
        return True
    echo("  Giving the VM an address so it can reach Google's package "
         "repository and PyPI. Inbound is still blocked to everything but "
         "the tunnel.")
    code, _out, err = _run(
        ["compute", "instances", "add-access-config", name,
         "--project", project, "--zone", zone,
         "--access-config-name=external-nat", "--quiet"])
    if code != 0:
        echo("  Could not add one: " + _tail(err, 2))
        return False
    return True


#: The last thing the startup script does, and the reason it is a `cat` and
#: not a `touch`: what the mount chain needs to know is not "did the script
#: reach the end" but "is gcsfuse actually here". This script has no `set -e`
#: -- deliberately, since a failed apt should not cost the machine its idle
#: timer -- so reaching the end proves nothing at all. It ran once against a
#: VM with no route to the internet, marked itself done, and left the mount
#: chain to wait five minutes for a package that was never coming.
STARTUP_DONE = f"""
sync
if command -v gcsfuse >/dev/null 2>&1; then
  echo ok > {STARTUP_MARK}
else
  echo no-gcsfuse > {STARTUP_MARK}
fi
"""

#: What the VM installs before anybody logs in. Runs as root at first boot,
#: which is the only moment there is root without asking the user for it --
#: gcsfuse comes from Google's own apt repository and installing it later,
#: from the session's own ssh, would need a sudo the OS Login user may not
#: have. `python3-venv` rides along for the same reason: the environment
#: Plexora is installed into is created by the session, but the tool that
#: creates it is not on a minimal Debian image.
STARTUP_SCRIPT = """#!/bin/bash
# Everything this script says, kept on the machine that said it. See
# STARTUP_LOG: the serial console is not a substitute, because Compute Engine
# stops serving it the moment the instance stops -- which is exactly what
# Plexora does to a VM whose first boot went wrong.
exec > >(tee -a __STARTUP_LOG__) 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive

# Swap, before anything that might need it. The smallest machine this preset
# offers has 4 GB of RAM, and the first connection asks pip to resolve and
# unpack scipy, scikit-image, pyarrow and the rest of it -- which on a small
# machine is an OOM kill and a Plexora that "just stopped" with nothing in the
# log to say why. A Debian cloud image has no swap at all; two gigabytes of the
# boot disk turns the smallest tier from a machine that might not finish
# installing into a slow one that does.
if ! swapon --show=NAME --noheadings | grep -q .; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# The suite is read off the machine rather than written in. Naming one here
# means the day IMAGE_FAMILY changes, apt is quietly pointed at a repository
# for a different Debian and gcsfuse stops existing.
CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")

# The key is saved as it is downloaded, armoured, and NOT run through
# `gpg --dearmor`. Google publishes it as an ASCII-armoured block and apt
# accepts one directly in `signed-by`, so the dearmor step converted a file
# that already worked into a dependency on a program that is not there: the
# Debian 13 image ships no gpg. What that cost was silent -- `gpg: not found`,
# no keyring written, the repository rejected as unsigned, and finally
# "E: Unable to locate package gcsfuse" several steps later.
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  -o /usr/share/keyrings/cloud.google.asc
echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc]" \
     "https://packages.cloud.google.com/apt gcsfuse-${CODENAME} main" \
  > /etc/apt/sources.list.d/gcsfuse.list
apt-get update -y
apt-get install -y gcsfuse python3-venv python3-pip fuse

cat > /usr/local/sbin/plexora-idle-shutdown <<'PLEXORA_IDLE'
#!/bin/sh
# Shut this VM down once nobody has been connected for IDLE_MINUTES.
#
# The last line of defence against an unwanted bill. Every other way Plexora
# has of stopping this machine runs on somebody's laptop, and a laptop can
# lose power, lose the network or be closed forever with a session still
# open. This runs here, so the only thing it needs in order to work is the
# thing being billed for.
#
# "Busy" is deliberately "an ssh session exists", not "a key was pressed":
# Plexora holds one open for the whole life of a connection, so an analysis
# running with the browser shut still counts as busy. The clock starts when
# the last session has gone.
IDLE_MINUTES=__IDLE_MINUTES__
STAMP=/var/lib/plexora-last-active
if who 2>/dev/null | grep -q . || pgrep -f '^sshd: .*@' >/dev/null 2>&1; then
  date +%s > "$STAMP"
  exit 0
fi
if [ ! -f "$STAMP" ]; then
  date +%s > "$STAMP"
  exit 0
fi
last=$(cat "$STAMP" 2>/dev/null || echo 0)
now=$(date +%s)
idle=$(( (now - last) / 60 ))
if [ "$idle" -ge "$IDLE_MINUTES" ]; then
  logger -t plexora "no session for ${idle}m; shutting down to stop billing"
  /sbin/shutdown -h now
fi
PLEXORA_IDLE
chmod 0755 /usr/local/sbin/plexora-idle-shutdown
date +%s > /var/lib/plexora-last-active

cat > /etc/systemd/system/plexora-idle-shutdown.service <<'PLEXORA_UNIT'
[Unit]
Description=Shut the VM down when no Plexora session has been connected

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/plexora-idle-shutdown
PLEXORA_UNIT

cat > /etc/systemd/system/plexora-idle-shutdown.timer <<'PLEXORA_TIMER'
[Unit]
Description=Check every few minutes whether this VM is idle

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=1min

[Install]
WantedBy=timers.target
PLEXORA_TIMER

systemctl daemon-reload
systemctl enable --now plexora-idle-shutdown.timer
""" + STARTUP_DONE

#: The token `STARTUP_SCRIPT` leaves for `startup_script()` to fill in. It sits
#: inside a quoted heredoc so the shell never touches it; the substitution is
#: Python's, before the file is ever written.
IDLE_PLACEHOLDER = "__IDLE_MINUTES__"
LOG_PLACEHOLDER = "__STARTUP_LOG__"


def startup_script(idle_minutes=IDLE_SHUTDOWN_MINUTES):
    """The first-boot script, with the idle window written into it.

    Only ever used by `create_instance`, and therefore only ever on a machine
    Plexora is renting. A VM the user already runs does not get this: it is
    theirs, it may be doing something Plexora cannot see, and a tool that
    installed a shutdown timer on somebody else's server would be doing
    something nobody asked for.
    """
    try:
        minutes = int(idle_minutes)
    except (TypeError, ValueError):
        minutes = IDLE_SHUTDOWN_MINUTES
    if minutes and minutes < MIN_IDLE_SHUTDOWN_MINUTES:
        minutes = MIN_IDLE_SHUTDOWN_MINUTES
    if not minutes:
        # Explicitly switched off: install nothing rather than a timer with an
        # absurd window, so `systemctl list-timers` on the VM tells the truth.
        head, _, _ = STARTUP_SCRIPT.partition("\ncat > /usr/local/sbin/")
        script = head + STARTUP_DONE
    else:
        script = STARTUP_SCRIPT.replace(IDLE_PLACEHOLDER, str(minutes))
    return script.replace(LOG_PLACEHOLDER, STARTUP_LOG)


def ensure_iap_firewall(project, echo=print):
    """Make sure something lets IAP reach port 22, best effort.

    Best effort on purpose. A default VPC usually already allows it, an
    organisation with its own network rules has an administrator who decided
    them, and a Plexora that treated "I could not create a firewall rule" as a
    failed connection would be refusing to try the thing that very likely
    already works. So: look, create if it is missing and we are allowed, and
    otherwise say exactly what to run and carry on.
    """
    try:
        rules = run_json(["compute", "firewall-rules", "list",
                          "--project", project],
                         what="Could not list firewall rules.")
    except GcloudError:
        return False
    for rule in rules or ():
        ranges = [str(item) for item in (rule.get("sourceRanges") or ())]
        if IAP_RANGE not in ranges:
            continue
        if str(rule.get("direction") or "INGRESS").upper() != "INGRESS":
            continue
        for allowed in rule.get("allowed") or ():
            if str(allowed.get("IPProtocol") or "").lower() != "tcp":
                continue
            ports = [str(port) for port in (allowed.get("ports") or ())]
            if not ports or "22" in ports:
                return True
    echo("  Adding a firewall rule so Google's IAP tunnel can reach port 22…")
    code, _out, err = _run(
        ["compute", "firewall-rules", "create", FIREWALL_RULE,
         "--project", project, "--direction=INGRESS", "--action=allow",
         "--rules=tcp:22", f"--source-ranges={IAP_RANGE}",
         "--description=Plexora: IAP TCP forwarding to SSH", "--quiet"],
        timeout=QUERY_TIMEOUT)
    if code == 0:
        return True
    echo("  Could not add it, so the connection may not get through. If it "
         "does not, ask whoever administers this project to run:")
    echo(f"    gcloud compute firewall-rules create {FIREWALL_RULE} "
         f"--project {project} --direction=INGRESS --action=allow "
         f"--rules=tcp:22 --source-ranges={IAP_RANGE}")
    return False


def ensure_public_deny(project, echo=print):
    """Close a tagged VM to everything except the IAP tunnel. Best effort.

    Created BEFORE the instance that needs it, never after: this is the rule
    that makes giving the machine a public address acceptable, and a machine
    that was briefly addressable while the rule was still being written would
    be a window nobody chose to open. `ensure_instance` orders the two, and a
    reused VM is tagged before it is given an address for the same reason.

    Scoped to `NET_TAG`, so the strongest thing it can possibly do is cut off
    a machine Plexora created. Ingress only -- the entire purpose of the
    address is egress, and egress rules are not touched here.
    """
    try:
        rules = run_json(["compute", "firewall-rules", "list",
                          "--project", project],
                         what="Could not list firewall rules.")
    except GcloudError:
        return False
    for rule in rules or ():
        if str(rule.get("name") or "") == DENY_RULE:
            return True
    echo("  Adding a firewall rule so only Google's IAP tunnel can reach the "
         "VM…")
    code, _out, err = _run(
        ["compute", "firewall-rules", "create", DENY_RULE,
         "--project", project, "--direction=INGRESS", "--action=deny",
         "--rules=all", "--source-ranges=0.0.0.0/0",
         f"--target-tags={NET_TAG}", f"--priority={DENY_PRIORITY}",
         "--description=Plexora: no inbound except the IAP tunnel", "--quiet"],
        timeout=QUERY_TIMEOUT)
    if code == 0:
        return True
    # Worth saying plainly. Everything else in this module that cannot create
    # something carries on quietly, but the thing that failed here is the
    # protection for an address the next line is about to hand out.
    echo("  Could not add it. The VM will have a public IP address protected "
         "only by this project's own firewall rules and by OS Login. To close "
         "it properly, run:")
    echo(f"    gcloud compute firewall-rules create {DENY_RULE} "
         f"--project {project} --direction=INGRESS --action=deny "
         f"--rules=all --source-ranges=0.0.0.0/0 --target-tags={NET_TAG} "
         f"--priority={DENY_PRIORITY}")
    echo(f"    (and see that {FIREWALL_RULE} exists to let the tunnel back in)")
    return False


def network_egress(project, region, subnet="default"):
    """Whether a VM with no public address could reach anything from here.

    Two ways out of a private subnet, and this reports both:

    - **Cloud NAT**, which is a full route to the internet and therefore the
      only one of the two that reaches PyPI.
    - **Private Google Access**, a subnet flag that routes Google's own
      domains -- so `packages.cloud.google.com` and Cloud Storage work, and
      `pip install plexora` still does not.

    Best effort, and says so by returning False for anything it could not
    read: this is used to explain a situation rather than to permit one, and
    an account that may not describe a subnet should not be blocked from
    connecting to a VM that works perfectly well.
    """
    nat = False
    access = False
    try:
        # Listed across the project and matched here rather than with
        # `--filter=region:(...)`, because a filter key gcloud does not
        # recognise matches NOTHING and says so only in a warning -- and the
        # failure that produces is the expensive direction: a project that
        # does have NAT, reported as having none, and a connection refused
        # for a reason that is not true.
        routers = run_json(["compute", "routers", "list", "--project", project],
                           what="Could not list Cloud Routers.")
        for router in routers or ():
            if not router.get("nats"):
                continue
            where = str(router.get("region") or "").rsplit("/", 1)[-1]
            if not where or where == region:
                nat = True
                break
    except GcloudError:
        pass
    try:
        described = run_json(["compute", "networks", "subnets", "describe",
                              subnet, "--project", project,
                              "--region", region],
                             what="Could not describe the subnet.")
        access = bool((described or {}).get("privateIpGoogleAccess"))
    except GcloudError:
        pass
    return {"nat": nat, "private_google_access": access}


def _no_egress_error(project, region):
    """The sentence for a private VM in a subnet with no way out.

    Said before the VM is created rather than after it has failed to install
    anything, because the failure this prevents is expensive in the two ways
    that matter: it costs eight minutes of somebody watching a progress line,
    and it leaves a broken machine and its disk behind to be billed for.
    """
    return (
        f"A VM with no public IP address cannot reach the internet from "
        f"{region} in “{project}”: the subnet there has no Cloud NAT and "
        f"Private Google Access is off, so the VM could not install Cloud "
        f"Storage FUSE or Plexora. Either turn “Give the VM a public IP "
        f"address” back on under Advanced — Plexora blocks all inbound "
        f"traffic to its own VMs, so the address is only a way out — or set "
        f"up Cloud NAT for that region:\n"
        f"    gcloud compute routers create plexora-nat-router "
        f"--project {project} --region {region} --network default\n"
        f"    gcloud compute routers nats create plexora-nat "
        f"--project {project} --region {region} --router "
        f"plexora-nat-router --auto-allocate-nat-external-ips "
        f"--nat-all-subnet-ip-ranges")


def create_instance(cfg, echo=print):
    """Ask for the VM this profile describes. Blocking.

    Nothing on the internet can reach this machine, and that is arranged in
    one of two ways depending on `external_ip`. Either the instance has no
    public address at all, or it has one and `DENY_RULE` drops every inbound
    packet to it that is not Google's IAP tunnel. The session goes in through
    the Identity-Aware Proxy either way, which is why OS Login is switched on
    here rather than left to a project default: IAP and OS Login are the pair
    that make "no open ports and no key files" work.

    The address is not a relaxation of that; it is the machine's way *out*.
    See `DENY_PRIORITY` for why a public IP with the door shut beats the
    alternative, and `_no_egress_error` for what happens without either.
    """
    project = _cfg(cfg, "project")
    zone = _cfg(cfg, "zone")
    name = _cfg(cfg, "vm_name")
    machine = _cfg(cfg, "machine_type") or DEFAULT_MACHINE_TYPE
    disk = int(_cfg(cfg, "boot_disk_gb", DEFAULT_BOOT_DISK_GB)
               or DEFAULT_BOOT_DISK_GB)
    idle = _cfg(cfg, "idle_shutdown_minutes", IDLE_SHUTDOWN_MINUTES)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(startup_script(idle))
        script = handle.name
    argv = [
        "compute", "instances", "create", name,
        "--project", project, "--zone", zone,
        "--machine-type", machine,
        "--image-family", image_family(cfg),
        "--image-project", _cfg(cfg, "image_project") or IMAGE_PROJECT,
        "--boot-disk-size", f"{disk}GB",
        "--boot-disk-type", "pd-balanced",
        # What `DENY_RULE` is scoped to. Set at create so that the rule --
        # which exists by now, because `ensure_instance` writes it first --
        # applies to this machine from its first packet.
        "--tags", NET_TAG,
        "--metadata", "enable-oslogin=TRUE",
        "--metadata-from-file", f"startup-script={script}",
        # Storage read/write so gcsfuse can authenticate as the instance
        # itself -- which is why no key material is copied to the VM and none
        # is stored here. Whether it may actually WRITE to this bucket is the
        # bucket's IAM, not this scope; the mount step checks and says so.
        "--scopes", "https://www.googleapis.com/auth/devstorage.read_write,"
                    "https://www.googleapis.com/auth/logging.write",
        # The proof of ownership `delete_instance` reads back. Written here
        # because here is the only moment Plexora can honestly claim to have
        # made this machine.
        "--labels", f"{OWNER_LABEL}={OWNER_VALUE}",
        "--quiet",
    ]
    if not wants_external_ip(cfg):
        argv.append("--no-address")
    if _cfg(cfg, "provisioning_model", DEFAULT_PROVISIONING) == PROVISIONING_SPOT:
        # STOP, not the DELETE that a legacy `--preemptible` VM gets. The whole
        # reason Spot is defensible as a default here is that being preempted
        # costs a reconnect rather than a rebuild -- and that is only true while
        # the boot disk, with `~/plexora-venv` on it, outlives the preemption.
        #
        # Nothing else is passed: SPOT implies `automaticRestart=false` and
        # `onHostMaintenance=TERMINATE`, and Compute Engine sets both itself.
        argv += ["--provisioning-model=SPOT",
                 "--instance-termination-action=STOP"]
    service_account = str(_cfg(cfg, "service_account") or "").strip()
    if service_account:
        argv += ["--service-account", service_account]
    code, _out, err = _run(argv, timeout=CREATE_TIMEOUT)
    if code != 0:
        sentence, recovery = _create_failure(cfg, err)
        raise GcloudError(sentence, _tail(err), recovery=recovery)
    echo(f"  The VM {name} exists.")
    return True


def _create_failure(cfg, err):
    """Why the create failed, and what to press about it.

    Returns `(sentence, recovery)`. The sentence carries the fix when Google's
    own text implies one; `recovery` is set only where that fix is a single
    edit to the saved profile, and is a `RECOVERY_*` key rather than a
    sentence -- see `GcloudError`.
    """
    lowered = str(err or "").lower()
    zone = _cfg(cfg, "zone")
    machine = _cfg(cfg, "machine_type")
    if "quota" in lowered:
        return (f"Google Cloud refused the VM: this project is at its quota "
                f"in {zone}. Ask for more CPUs there, or choose a smaller "
                f"machine type than {machine}.", "")
    if "does not have enough resources" in lowered or "zone_resource" in lowered:
        # Spot capacity is the first thing a zone runs out of, and it runs out
        # for spot long before it runs out for anybody paying full price -- so
        # on a spot request the likeliest fix is not a different zone at all.
        # It is also the one fix that is a button: same zone, same machine,
        # same everything, bought outright. Hence the recovery key.
        spot = (_cfg(cfg, "provisioning_model", DEFAULT_PROVISIONING)
                == PROVISIONING_SPOT)
        if spot:
            return (f"{zone} has no spare {machine} to give away as a Spot VM "
                    f"right now. Ask for a Standard one instead, or pick "
                    f"another zone or machine type.", RECOVERY_STANDARD)
        return (f"{zone} has no {machine} capacity right now. Pick another "
                f"zone, or a different machine type.", "")
    if "compute.googleapis.com" in lowered or "api has not been used" in lowered:
        return ("The Compute Engine API is not enabled on this project. "
                "Enable it once, then connect again:\n"
                f"    gcloud services enable compute.googleapis.com "
                f"--project {_cfg(cfg, 'project')}", "")
    if "permission" in lowered or "forbidden" in lowered or "403" in lowered:
        return ("This account is not allowed to create VMs in "
                f"“{_cfg(cfg, 'project')}”. It needs the Compute Instance "
                "Admin role there.", "")
    if "billing" in lowered:
        return (f"“{_cfg(cfg, 'project')}” has no billing account attached, "
                "so Compute Engine will not start a VM in it.", "")
    return f"Could not create the VM “{_cfg(cfg, 'vm_name')}”.", ""


def start_instance(cfg, block=True):
    """Start a VM that exists and is stopped.

    Blocking for the connection ladder, which has nothing to do until the
    machine is up anyway. Not blocking for the button on the Settings page:
    starting takes the better part of a minute, and an HTTP request that held
    a worker for it would be a page that appears to have frozen while doing
    exactly what it was asked.
    """
    argv = ["compute", "instances", "start", _cfg(cfg, "vm_name"),
            "--project", _cfg(cfg, "project"), "--zone", _cfg(cfg, "zone"),
            "--quiet"]
    if not block:
        argv.append("--async")
    code, _out, err = _run(argv, timeout=START_TIMEOUT)
    if code != 0:
        raise GcloudError(
            f"Could not start the VM “{_cfg(cfg, 'vm_name')}”.", _tail(err))
    return True


def stop_instance(cfg, block=True):
    """Stop the VM. The disk survives, and so does everything in the bucket.

    The middle answer to "what happens when I disconnect": a stopped VM costs
    only its disk, and starting it again is much faster than creating one,
    because the environment Plexora installed is still on it.

    "Only its disk" is not "nothing", and the UI says so rather than letting
    somebody discover it: at the default 20 GB that is roughly $2 a month,
    every month, until the VM is deleted.
    """
    argv = ["compute", "instances", "stop", _cfg(cfg, "vm_name"),
            "--project", _cfg(cfg, "project"), "--zone", _cfg(cfg, "zone"),
            "--quiet"]
    if not block:
        # For teardown paths that are themselves racing something: an atexit
        # handler, or a session thread ending while the app shuts down. Ask
        # Google to stop the machine and do not wait to watch it happen --
        # waiting is what turns "quit Plexora" into a minute of nothing, and
        # the stop is already committed on their side by the time this
        # returns.
        argv.append("--async")
    code, _out, err = _run(argv, timeout=QUERY_TIMEOUT)
    if code != 0:
        raise GcloudError(
            f"Could not stop the VM “{_cfg(cfg, 'vm_name')}”.", _tail(err))
    return True


def delete_instance(cfg, block=True):
    """Delete a VM **Plexora created**, and its boot disk. Never the bucket.

    Read the argv: it names a project, a zone and an instance, and there is no
    branch of this module that can put a bucket into it. That is the whole
    guarantee, and it is structural rather than a promise -- the compute is
    rented and the data is not, so the destructive verb this file has is about
    a machine and the file has no verb about storage at all.

    The second guarantee is here rather than in the caller: the instance is
    described first, and deleted only if it carries the label
    `create_instance` wrote. A profile pointed at a VM the user already runs
    cannot delete it, and neither can a hand-edited profile, a stale record or
    a mistaken button -- because the permission is read off the machine rather
    than off the thing asking.

    `block=False` for the teardown paths, which run on a dying session thread
    and inside an atexit handler: the instruction is committed on Google's side
    by the time `--async` returns, and waiting for the delete to finish is how
    "quit Plexora" becomes a minute of a window that will not close. The
    describe above it is NOT skipped for those -- the ownership check is the
    point of this function, and a fast delete of the wrong machine would be the
    one bug this whole design exists to make impossible.
    """
    project = _cfg(cfg, "project")
    zone = _cfg(cfg, "zone")
    name = _cfg(cfg, "vm_name")
    described = instance(project, zone, name)
    if described is None:
        raise GcloudError(
            f"There is no VM called “{name}” in {zone} to delete. It may "
            "already be gone.")
    if not made_by_plexora(described):
        raise GcloudError(
            f"“{name}” was not created by Plexora, so Plexora will not delete "
            f"it. Machines you already ran are yours to remove, in the Google "
            f"Cloud console or with `gcloud compute instances delete {name} "
            f"--zone {zone}`.")
    argv = ["compute", "instances", "delete", _cfg(cfg, "vm_name"),
            "--project", _cfg(cfg, "project"), "--zone", _cfg(cfg, "zone"),
            "--quiet"]
    if not block:
        argv.append("--async")
    code, _out, err = _run(argv, timeout=START_TIMEOUT)
    if code != 0:
        raise GcloudError(
            f"Could not delete the VM “{_cfg(cfg, 'vm_name')}”.", _tail(err))
    return True


def ssh_argv(cfg, command=None, ssh_flags=()):
    """`gcloud compute ssh`, which is the whole of how this session is reached.

    Not plain `ssh` with a tunnel opened beside it. gcloud owns three things
    this end would otherwise have to reimplement: it registers the OS Login
    key, it knows the login name Google derived from the account's email, and
    it carries the connection over IAP into a VM with no address. Everything
    after `--` reaches the underlying ssh untouched, which is what lets the
    same `-L` forward, the same keepalives and the same `-t` be used here as
    on a plain host.
    """
    argv = ["gcloud", "compute", "ssh", _cfg(cfg, "vm_name"),
            "--project", _cfg(cfg, "project"),
            "--zone", _cfg(cfg, "zone"),
            "--tunnel-through-iap", "--quiet"]
    if command is not None:
        argv += ["--command", command]
    flags = list(ssh_flags or ())
    if flags:
        argv += ["--", *flags]
    return argv


def ssh_probe(cfg, command=None, mark="PLEXORA_SSH_OK"):
    """One "is it answering yet" round trip. `(ok, detail)`.

    Doubles as the thing that makes the real connection quick: the first
    `gcloud compute ssh` to a new VM uploads the OS Login key and learns the
    host key, and doing that here means the session's own ssh -- the one whose
    output somebody is watching -- is not the one waiting on it.

    `command`/`mark` are how `ensure_instance` asks a second question with the
    same machinery. Both questions want the same thing from a failure -- retry
    rather than fail -- and the retrying happens here, on this side of the
    connection, which is the entire point of asking them here.
    """
    argv = ssh_argv(cfg, command=command or f"echo {mark}",
                    ssh_flags=["-o", "ConnectTimeout=20"])
    try:
        code, out, err = _RUNNER(argv, timeout=SSH_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "the SSH probe timed out"
    except FileNotFoundError:
        raise GcloudError(
            "The Google Cloud CLI (`gcloud`) is not installed on this "
            "machine.") from None
    if code == 0 and mark in (out or ""):
        return True, ""
    return False, _tail(err, 3)


#: What the marker probe runs. `test -f` rather than `command -v gcsfuse`,
#: because the question is "has the startup script finished", and a script
#: that finished having failed to install anything still answers it -- the
#: mount chain is what deals with that, and it can only deal with it once it
#: has a connection to deal with it through.
STARTUP_PROBE = f"test -f {STARTUP_MARK} && echo PLEXORA_STARTUP_DONE"


def await_startup(cfg, echo=print):
    """Wait until the VM's first-boot script has stopped working. Best effort.

    Separate from the ssh readiness loop above it, and after it, because the
    two are different questions with the same shape. sshd answers within
    seconds of boot; the startup script is still running `apt-get` minutes
    later, and on the smallest machine this preset offers that apt install is
    most of what the machine has. Handing the session's own ssh to a host in
    that state is how a connection that probed fine seconds ago comes back
    `Connection closed`.

    The mount chain waits for the same marker, but it can only start waiting
    once ssh has succeeded -- which is the wrong side of the door. Here the
    wait is on the side that retries.

    Never fatal. A marker that never arrives means the startup script hung or
    is from a Plexora too old to write one, and in both cases the mount chain
    knows how to install gcsfuse itself. What this buys is not a guarantee; it
    is not walking into the machine while it is busy.
    """
    deadline = _now() + STARTUP_READY_TIMEOUT
    said = False
    while True:
        ok, _detail = ssh_probe(cfg, command=STARTUP_PROBE,
                                mark="PLEXORA_STARTUP_DONE")
        if ok:
            return True
        if _now() > deadline:
            echo("  The VM is still setting itself up. Carrying on anyway — "
                 "the mount step installs what it needs if it has to.")
            return False
        if not said:
            # Said once, not once per probe: this is the longest wait in a
            # first connection and somebody is watching it, but nine copies
            # of the same line is a log that looks stuck rather than busy.
            echo("  Waiting for the VM to finish installing Cloud Storage "
                 "FUSE. On a first connection this takes a few minutes.")
            said = True
        _sleep(15)


def ensure_instance(cfg, echo=print):
    """Have the VM this profile names, running and reachable. The whole ladder.

    Reuse, then start, then create -- in that order, and each announced,
    because they cost wildly different amounts of somebody's time and money
    and a connection that silently created a second VM would be a bill nobody
    agreed to. The state is read from Compute Engine rather than remembered
    here: a VM somebody stopped in the console yesterday is stopped, whatever
    this end last saw.

    For a profile pointing at a machine the user already runs, the ladder has
    no top rung: an absent VM is an error naming the VM, not an invitation to
    build one. Being asked to connect to `analysis-box` is not permission to
    create something called `analysis-box`.

    Returns what it did -- `"created"`, `"started"` or `"reused"` -- because
    the caller's teardown depends on it. Cleaning up after a connection that
    failed means stopping a machine this attempt brought up, and not touching
    one that was already running when we arrived.
    """
    project = _cfg(cfg, "project")
    zone = _cfg(cfg, "zone")
    name = _cfg(cfg, "vm_name")
    machine = _cfg(cfg, "machine_type") or DEFAULT_MACHINE_TYPE
    rented = _cfg(cfg, "vm_source", VM_PLEXORA) != VM_EXISTING
    if not available():
        raise GcloudError(
            "The Google Cloud CLI (`gcloud`) is not installed on this "
            "machine. Install it from cloud.google.com/sdk and sign in with "
            "`gcloud auth login`.")
    if not (project and zone and name):
        raise GcloudError(
            "This connection is missing its Google Cloud project, zone or VM "
            "name. Edit it in Settings and connect again.")
    signed_in = account()
    if not signed_in:
        raise GcloudError(
            "Nobody is signed in to Google Cloud on this machine. Run "
            "`gcloud auth login`, or open “Add a server” and press “Sign in "
            "with Google”.")
    echo(f"  Signed in to Google Cloud as {signed_in}.")

    # Both rules before any machine exists to need them. The deny rule in
    # particular has to be in place before the address is, which is an
    # ordering `create_instance` and `repair_egress` both depend on.
    public = rented and wants_external_ip(cfg)
    ensure_iap_firewall(project, echo)
    if public:
        ensure_public_deny(project, echo)

    found = instance(project, zone, name)
    status = (found or {}).get("status") or ""
    action = "reused"
    if found is None and not rented:
        raise GcloudError(
            f"There is no VM called “{name}” in {zone}. This connection is "
            f"set to use a machine you already run, so Plexora will not "
            f"create one. Check the name and zone in Settings, or switch the "
            f"connection to let Plexora provide the VM.")
    # Before the start, not after it. Compute Engine runs the startup script
    # on every boot, so a VM repaired while it is still stopped comes up with
    # a route out and installs what it was always meant to; repaired after the
    # start, it boots into the same failure one last time and has to be put
    # right by the mount chain's fallback instead.
    if found is not None and public:
        repair_egress(cfg, found, echo)

    if found is None:
        if rented and not public:
            # Checked here rather than trusted, and only on the branch that is
            # about to spend money. A private VM in a subnet with no way out
            # boots, answers the tunnel, looks entirely healthy, and cannot
            # install a single package -- so the cheapest possible moment to
            # find out is before it is asked for.
            reach = network_egress(project, region_of_zone(zone) or zone)
            if not (reach["nat"] or reach["private_google_access"]):
                raise GcloudError(_no_egress_error(project,
                                                   region_of_zone(zone)))
            if not reach["nat"]:
                echo("  Note: this subnet reaches Google's own services but "
                     "not the rest of the internet, so installing Plexora "
                     "from PyPI on the VM will fail. Cloud NAT or a public "
                     "address is what fixes that.")
        echo(f"  No VM called {name} yet. Requesting a new {machine} in "
             f"{zone}; this takes a minute or two.")
        create_instance(cfg, echo)
        action = "created"
    elif status == "RUNNING":
        echo(f"  Reusing the VM {name}, which is already running in {zone}.")
    elif status in ("TERMINATED", "STOPPED", "SUSPENDED"):
        echo(f"  Starting the VM {name}, which was stopped.")
        start_instance(cfg)
        action = "started"
    else:
        echo(f"  The VM {name} is {status.lower() or 'busy'}; waiting for it.")

    if found is not None and not can_reach_storage(found):
        # Said before the mount rather than after, because the fix is on the
        # instance and not on the bucket: gcsfuse authenticates as the VM, and
        # a VM whose scopes exclude Cloud Storage will get a 403 no matter how
        # the bucket's IAM is written.
        echo(f"  Note: {name} was created without access to Cloud Storage, so "
             f"the mount may be refused. Stopping it and setting its access "
             f"scope to “Allow full access to all Cloud APIs” is the fix.")

    echo("  Waiting for the VM to accept a connection…")
    deadline = _now() + SSH_READY_TIMEOUT
    detail = ""
    while True:
        ok, detail = ssh_probe(cfg)
        if ok:
            # Answering is not the same as ready. For a machine Plexora
            # rented there is a second question worth asking before the
            # session's own ssh is spent on it -- see `await_startup`.
            if rented:
                await_startup(cfg, echo)
            echo(f"  {name} is ready.")
            return action
        if _now() > deadline:
            break
        _sleep(10)
    raise GcloudError(
        f"The VM {name} did not accept a connection within "
        f"{SSH_READY_TIMEOUT:g}s. It may still be starting, or this account "
        f"may need the IAP-secured Tunnel User role on “{project}”.",
        detail)


# -- the mount --------------------------------------------------------------

#: What may appear in a path typed into the mount box. Deliberately narrow:
#: this string is spliced into a shell command line on the VM, so the set of
#: characters it may contain is the set that cannot mean anything there.
_PATH_RE = re.compile(r"^~?[A-Za-z0-9_./-]+$")


def valid_mount_path(path):
    text = str(path or "").strip()
    return bool(text) and bool(_PATH_RE.match(text)) and ".." not in text


def _path_expr(path):
    """A user-typed remote path as a shell expression that expands `~`.

    `shlex.quote` would be wrong for the default value: quoting `~/plexora-data`
    gives a literal directory called `~`, in whatever the working directory
    happened to be. So the tilde is turned into `$HOME` inside double quotes,
    which expands, and everything else is quoted normally.
    """
    text = str(path or "").strip()
    if text.startswith("~/"):
        return '"$HOME/' + text[2:] + '"'
    if text == "~":
        return '"$HOME"'
    return shlex.quote(text)


#: How long to wait for the first-boot startup script to finish installing
#: gcsfuse. The mount is the first thing the session's ssh does, and on a VM
#: created ninety seconds ago apt may still be running -- so this waits rather
#: than failing, which is the difference between a slow first connection and a
#: first connection that never works.
#:
#: The ceiling, not the usual case: the loop watches `STARTUP_MARK` and stops
#: the moment the script has finished, whatever it managed to install. Only a
#: startup script that is still running uses the full five minutes.
GCSFUSE_WAIT_TRIES = 60
GCSFUSE_WAIT_SECONDS = 5

#: Printed by the prep chain when the bucket mounted read-only. A marker
#: rather than a failure -- see `prepare_command_line`. The same string is
#: spelled out in `connect.MOUNT_READONLY_MARK`, which is the end that reads
#: it: connect.py is standalone-loadable and may not import this module, so
#: the two are pinned to each other by a test rather than by an import.
MOUNT_READONLY_MARK = "PLEXORA_MOUNT_READONLY"

#: Adding Google's apt repository and installing from it, guarded by `sudo -n`
#: so that a user without passwordless sudo gets a sentence rather than a
#: password prompt on a connection nobody is watching a terminal for.
#:
#: Used by both branches of `_gcsfuse_step`. It used to be the BYO branch's
#: alone, and the rented branch fell back to a bare `apt-get install gcsfuse`
#: -- which cannot work on the machine that needs it most. A VM whose startup
#: script failed has the repository listed in `sources.list.d` and no key for
#: it, so the only package list apt has is one that has never mentioned
#: gcsfuse. Repeating the whole recipe is what lets a half-built VM finish
#: building itself.
_GCSFUSE_INSTALL = (
    "sudo -n sh -c '"
    # `/etc/os-release` rather than `lsb_release`, which is a package and not
    # a guarantee, and rather than a codename written in here, which would be
    # a guess about a machine this branch exists precisely because we did not
    # build.
    ". /etc/os-release; "
    # Armoured key straight to disk -- no `gpg --dearmor`. See the startup
    # script for what that step cost on an image with no gpg on it.
    "curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg "
    "-o /usr/share/keyrings/cloud.google.asc && "
    "echo \"deb [signed-by=/usr/share/keyrings/cloud.google.asc] "
    "https://packages.cloud.google.com/apt gcsfuse-$VERSION_CODENAME main\" "
    "> /etc/apt/sources.list.d/gcsfuse.list && "
    "apt-get update -y && apt-get install -y gcsfuse fuse"
    f"' > {INSTALL_LOG} 2>&1 || true"
)

#: What to say when gcsfuse is not there and could not be put there.
#:
#: Deliberately short, and deliberately not a diagnosis. An earlier version of
#: this named the cause it thought was likeliest -- no route to the internet
#: -- which was right once and then wrong, and a confident wrong sentence is
#: worse than none: it sends somebody to check a network that was fine. What
#: follows this sentence is what the machine actually said, which is the
#: thing that was missing. No apostrophes: it is spliced into a
#: single-quoted shell string.
_NO_GCSFUSE = (
    "Cloud Storage FUSE could not be installed on this VM, so there is "
    "nothing to mount the bucket with. What apt said is below. A common "
    "cause is a VM with no route to the internet -- one with no public IP "
    "address needs Cloud NAT in its region -- but read the log first."
)


def _gcsfuse_step(rented):
    """Have gcsfuse, by whichever route this machine's history allows.

    Both branches end in the same test, so the `&&` chain behind them cannot
    tell the difference: either gcsfuse is on PATH by the end of this or the
    connection stops here with something to read.

    What differs is only whether to wait first. On a rented VM the startup
    script is very likely mid-`apt-get` right now, and starting a second one
    alongside it is its own kind of failure -- so that branch waits for the
    script's own marker before trying anything. On a machine the user already
    runs there is no startup script and never will be, so waiting for one
    would be five minutes of nothing.
    """
    wait = ""
    if rented:
        wait = (f"n=0; while [ $n -lt {GCSFUSE_WAIT_TRIES} ] && "
                f"[ ! -f {STARTUP_MARK} ] && "
                "! command -v gcsfuse >/dev/null 2>&1; do "
                f"sleep {GCSFUSE_WAIT_SECONDS}; n=$((n+1)); done; ")
    return ("{ command -v gcsfuse >/dev/null 2>&1 || { "
            "echo 'Installing Cloud Storage FUSE on the VM…'; "
            f"{wait}"
            "command -v gcsfuse >/dev/null 2>&1 || { "
            f"{_GCSFUSE_INSTALL}; }}; }}; "
            "command -v gcsfuse >/dev/null 2>&1 || { "
            f"echo '{_NO_GCSFUSE}' >&2; "
            # The evidence, from both places it could be. Neither may exist --
            # a machine Plexora did not build has no startup log, and a
            # fallback that never ran leaves no install log -- so both are
            # tried and neither is allowed to fail the chain on its own.
            "echo '--- what the VM said while installing it ---' >&2; "
            # `>&2` BEFORE `2>/dev/null`, and the order is the whole thing:
            # redirections are applied left to right, so `2>/dev/null >&2`
            # points stdout at wherever fd2 already goes -- which by then is
            # /dev/null. That spelling printed a header, a footer and nothing
            # in between, which is worse than not trying.
            f"tail -n 25 {INSTALL_LOG} >&2 2>/dev/null || true; "
            f"tail -n 25 {STARTUP_LOG} >&2 2>/dev/null || true; "
            "echo '--- end ---' >&2; "
            "false; }; }")


def _python_check(rented):
    """Refuse the install on a Python too old for Plexora, in one sentence.

    Two sentences, in fact, because the fix depends on whose machine it is. On
    a VM Plexora built the image is Plexora's choice and the answer is to
    throw the machine away and let it build a newer one -- the whole point of
    a rented VM is that this costs nothing but a minute. On a machine the user
    already runs, the image is theirs and so is the decision.
    """
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    fix = ("Delete the VM in Settings and connect again — Plexora will build "
           "a new one on a current image."
           if rented else
           "Install a newer Python on that machine, or point this connection "
           "at one that has it.")
    return (
        "{ python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= "
        f"{MIN_PYTHON} else 1)' 2>/dev/null || {{ "
        f'echo "Plexora needs Python {want} or newer, and this machine has '
        f'$(python3 -V 2>&1 || echo none). {fix}" >&2; false; }}; }}'
    )


def prepare_command_line(cfg):
    """The one shell line that mounts the bucket and readies the environment.

    Chained ahead of the launch in the SAME ssh, for the same reasons the
    install is (see `connect.install_prefixed`): one login, and `&&` meaning
    that nothing starts from a machine where the mount failed. Each step
    announces itself, because these are the sentences somebody watches during
    the longest part of a first connection.

    The write check is a warning, not a gate. A read-only bucket is a
    perfectly ordinary thing to be given -- somebody else's published atlas --
    and Plexora can open images from one; what it cannot do is save a figure
    there, and saying so early is better than a permission error four screens
    later.

    Getting gcsfuse there differs by whose machine this is, which is why the
    first step is built rather than fixed -- see `_gcsfuse_step`.
    """
    mount = _path_expr(_cfg(cfg, "mount_path") or DEFAULT_MOUNT_PATH)
    name = str(_cfg(cfg, "bucket") or "").strip()
    if not valid_bucket_name(name):
        raise GcloudError(f"“{name}” is not a Cloud Storage bucket name.")
    bucket_name = shlex.quote(name)
    venv = '"$HOME/plexora-venv"'

    rented = _cfg(cfg, "vm_source", VM_PLEXORA) != VM_EXISTING
    staging = _path_expr(GCSFUSE_TEMP)
    steps = [
        _gcsfuse_step(rented),
        f"mkdir -p {mount} {staging}",
        f"echo 'Mounting gs://{name} at {_cfg(cfg, 'mount_path')}…'",
        # Already mounted is success, not a second mount: reconnecting to a
        # running VM finds the bucket where the last session left it.
        #
        # `--temp-dir` is named rather than defaulted for the reason given at
        # GCSFUSE_TEMP: writing an object stages the whole of it somewhere
        # first, and "somewhere" deciding itself is how that becomes RAM.
        f"{{ mountpoint -q {mount} 2>/dev/null || "
        f"gcsfuse --implicit-dirs --temp-dir {staging} "
        f"{bucket_name} {mount}; }}",
        "echo 'Verifying data access…'",
        f"ls {mount} >/dev/null",
        # A redirect rather than `touch`, because `touch` asks for two things
        # and only one of them is the question. It creates the object and then
        # calls `utimensat` to set its timestamps, which a bucket mount does
        # not necessarily implement -- so a `touch` that fails can mean "this
        # bucket is read-only" or merely "you cannot set mtime on an object",
        # and reporting the second as the first sends somebody to the IAM page
        # to fix a permission they already have.
        f"{{ : > {mount}/.plexora-write-check 2>/dev/null && "
        f"rm -f {mount}/.plexora-write-check || echo {MOUNT_READONLY_MARK}; }}",
        # First connection only. `python3 -m venv` is on the image because the
        # startup script put it there; the pip that follows is the one thing
        # here that takes minutes, and it says so.
        #
        # The version is checked first because pip's way of saying it is a
        # wall of "Ignored the following versions that require a different
        # python version" ending in "No matching distribution found for
        # plexora", which reads as "this package does not exist" rather than
        # as "this machine is too old".
        f"{{ [ -x {venv}/bin/plexora ] || {{ "
        "echo 'Setting Plexora up on the VM; this takes a few minutes…'; "
        f"{_python_check(rented)} && "
        f"python3 -m venv {venv} && {venv}/bin/pip install "
        "--progress-bar off --upgrade pip plexora; }; }",
    ]
    return " && ".join(steps)


def profile(**values):
    """One `extra["gcloud"]` record, with the defaults filled in.

    Schema v4. **There is no credential field and there must never be one**:
    what is stored is which project, which bucket and which machine to ask
    for, all of which is a description of a connection rather than a way in.
    Google's own credential store is where the way in lives.

    v4 replaced the `stop_vm_on_disconnect` boolean with `on_exit`, which is
    the same question with the answer it was always missing: a VM that is
    deleted at the end of a session costs nothing at all afterwards, and a
    two-valued switch could only offer "keep paying for compute" or "keep
    paying for the disk". Records written before this are read by
    `exit_action`, which understands both.

    Two invariants are enforced here rather than trusted to callers, because
    both of them cost money or trust when they are got wrong:

    - A rented VM stops when nobody is connected. That is the default, and it
      is the conservative one: the mistake it prevents (a 16-core machine
      billing all weekend) is unrecoverable, and the mistake it causes (a
      forty-second wait on the next connect) is not.
    - A VM the user already runs is never deleted, never has a shutdown timer
      installed and is never given an address. It is not ours, it may be doing
      something we cannot see, and its network is somebody else's decision.
      Stopping one IS allowed, because that is a person answering a question
      about their own machine on this form -- but deleting is not on the menu
      at any price. `exit_action` makes the same refusal on the way out, and
      `delete_instance` makes it a third time against the instance's own label.
    """
    out = {
        "version": 4,
        "account": "",
        "project": "",
        "bucket": "",
        "bucket_location": "",
        "region": DEFAULT_REGION,
        "zone": "",
        "machine_type": DEFAULT_MACHINE_TYPE,
        "provisioning_model": DEFAULT_PROVISIONING,
        "vm_name": "",
        "vm_source": VM_PLEXORA,
        # A way out, not a way in. See DENY_PRIORITY.
        "external_ip": True,
        "mount_path": DEFAULT_MOUNT_PATH,
        "boot_disk_gb": DEFAULT_BOOT_DISK_GB,
        "image_family": IMAGE_FAMILY,
        "image_project": IMAGE_PROJECT,
        "service_account": "",
        "auto_create_firewall": True,
        "on_exit": DEFAULT_EXIT,
        "idle_shutdown_minutes": IDLE_SHUTDOWN_MINUTES,
    }
    out.update({key: value for key, value in values.items() if key in out})
    if out["vm_source"] not in (VM_PLEXORA, VM_EXISTING):
        out["vm_source"] = VM_PLEXORA
    if not valid_provisioning(out["provisioning_model"]):
        out["provisioning_model"] = DEFAULT_PROVISIONING
    if not valid_exit(out["on_exit"]):
        out["on_exit"] = DEFAULT_EXIT
    if out["vm_source"] == VM_EXISTING:
        if out["on_exit"] == EXIT_DELETE:
            out["on_exit"] = EXIT_LEAVE
        out["idle_shutdown_minutes"] = 0
        out["external_ip"] = False
        # Not a lie about somebody else's machine: how it was bought was
        # settled before Plexora ever heard of it, and this field is only ever
        # read by `create_instance`.
        out["provisioning_model"] = PROVISIONING_STANDARD
    return out
