"""Google Cloud, without a Google Cloud account.

Everything `plexora.gcloud` does goes through one subprocess seam, so the whole
module can be driven on canned JSON and the argv it produced recorded and
pinned. That matters more here than in most places: this is the one part of
Plexora that creates things somebody is billed for, and the argv is the part
that has to be exactly right on a machine nobody can reach from CI.

Two promises are pinned rather than described:

**Deleting the VM cannot delete the bucket.** Not "does not" -- cannot. The
lifecycle argv never names the bucket, and the module has no storage-deletion
verb for anything to call.

**Nothing here stores a credential.** The saved record is a description of a
connection: which project, which bucket, which machine type. gcloud's own
credential store keeps the way in.
"""

import json
import subprocess

import pytest

from plexora import gcloud


# -- the seam ----------------------------------------------------------------


class FakeRunner:
    """A gcloud that answers from a script and remembers what it was asked."""

    def __init__(self, answers=None, default=(0, "", "")):
        #: Keyed on a substring of the joined argv, first match wins. A list
        #: as the value is consumed one call at a time, which is how the reuse
        #: ladder's "absent, then present" sequences are written.
        self.answers = list(answers or [])
        self.default = default
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for entry in self.answers:
            match, result = entry[0], entry[1]
            if match in joined:
                if isinstance(result, list):
                    return result.pop(0) if result else self.default
                return result
        return self.default

    @property
    def argv(self):
        return [" ".join(call) for call in self.calls]


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(gcloud, "_RUNNER", fake)
    monkeypatch.setattr(gcloud, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(gcloud, "_sleep", lambda seconds: None)
    return fake


CFG = {
    "project": "my-project",
    "zone": "us-east1-b",
    "vm_name": "plexora-gcp",
    "machine_type": "e2-highmem-16",
    "bucket": "my-imaging-bucket",
    "mount_path": "~/plexora-data",
    "boot_disk_gb": 50,
}


# -- reading what somebody has -----------------------------------------------


def test_the_signed_in_account_is_read_rather_than_asked_for(runner):
    runner.answers = [("auth list", (0, json.dumps(
        [{"account": "aj@example.com", "status": "ACTIVE"}]), ""))]
    assert gcloud.account() == "aj@example.com"
    # Read-only, and it never sees a token: what comes back is an email
    # address, which is the only part of a sign-in Plexora has any use for.
    assert "auth list" in runner.argv[0]
    assert "--format=json" in runner.argv[0]


def test_nobody_signed_in_is_an_answer_rather_than_an_error(runner):
    runner.answers = [("auth list", (0, "[]", ""))]
    assert gcloud.account() is None
    # And so is a gcloud that refuses: the form's response to both is the same
    # button, and a red box in front of it would be a red box in front of the
    # fix.
    runner.answers = [("auth list", (1, "", "ERROR: something"))]
    assert gcloud.account() is None


def test_buckets_come_back_with_the_region_each_one_implies(runner):
    runner.answers = [("storage buckets list", (0, json.dumps([
        {"name": "b-two", "location": "US-EAST1", "location_type": "region"},
        {"name": "a-one", "location": "US", "location_type": "multi-region"},
    ]), ""))]
    found = gcloud.buckets("my-project")
    assert [entry["name"] for entry in found] == ["a-one", "b-two"]
    # The join the form would otherwise ask somebody to do by hand in the
    # console -- which is exactly where the mistake that costs egress is made.
    assert found[1]["region"] == "us-east1"
    assert found[1]["exact"] is True
    assert found[0]["region"] == "us-central1"
    assert found[0]["exact"] is False


def test_a_bucket_that_is_not_there_says_so_in_words_somebody_can_act_on(runner):
    runner.answers = [("storage buckets describe",
                       (1, "", "ERROR: (gcloud) NOT_FOUND: 404 bucket"))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.bucket("my-project", "nope-bucket")
    assert "no bucket called gs://nope-bucket" in str(raised.value)


def test_a_bucket_this_account_cannot_read_names_the_role_it_needs(runner):
    runner.answers = [
        ("storage buckets describe", (1, "", "ERROR: 403 does not have "
                                             "permission")),
        # Refused twice: the metadata AND the objects. That second refusal is
        # what makes this a bucket nobody can use rather than a public one --
        # see the test below.
        ("storage objects list", (1, "", "ERROR: 403 does not have "
                                         "permission")),
    ]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.bucket("my-project", "someone-elses")
    assert "Storage Object Viewer" in str(raised.value)


def test_a_public_bucket_is_readable_even_though_it_cannot_be_described(runner):
    """Somebody else's published atlas is world-READABLE, and describing it is
    not part of that: `allUsers` gets Storage Object Viewer, which is objects,
    not metadata. So a 403 from `buckets describe` is not proof the bucket is
    unusable -- it is a reason to ask the question the mount will actually ask.

    What comes back says so, because the region cannot be read either and the
    form has to explain why the field that filled itself everywhere else is
    blank on this one."""
    runner.answers = [
        ("storage buckets describe", (1, "", "ERROR: 403 does not have "
                                             "permission")),
        ("storage objects list", (0, "[]", "")),
    ]
    found = gcloud.bucket("my-project", "somebody-elses-atlas")
    assert found["name"] == "somebody-elses-atlas"
    assert found["public"] is True
    # No location, and therefore nothing to claim about a region: `exact` False
    # is what stops the form saying "detected from your bucket" about a guess.
    assert found["location"] == ""
    assert found["exact"] is False
    # One object, not a listing: the question is whether the read is permitted,
    # and a bucket with four million objects in it should answer as fast as one
    # with a single object.
    asked = [line for line in runner.argv if "storage objects list" in line][0]
    assert "--limit=1" in asked


def test_only_zones_that_are_up_are_offered(runner):
    runner.answers = [("compute zones list", (0, json.dumps([
        {"name": "us-east1-b", "status": "UP"},
        {"name": "us-east1-c", "status": "DOWN"},
    ]), ""))]
    assert gcloud.zones("my-project", "us-east1") == ["us-east1-b"]
    assert gcloud.pick_zone("my-project", "us-east1") == "us-east1-b"


# -- matching compute to data ------------------------------------------------


def test_a_regional_bucket_has_a_region_and_a_multi_region_has_a_guess():
    """`exact` is the whole point. A bucket in US-EAST1 has a region, and
    computing anywhere else is a mistake worth warning about; a bucket in US
    does not, and warning about it would be warning about nothing."""
    assert gcloud.region_for_bucket_location("US-EAST1") == ("us-east1", True)
    assert gcloud.region_for_bucket_location("europe-west4") == ("europe-west4",
                                                                True)
    assert gcloud.region_for_bucket_location("US") == ("us-central1", False)
    assert gcloud.region_for_bucket_location("EU") == ("europe-west1", False)
    assert gcloud.region_for_bucket_location("NAM4") == ("us-central1", False)
    assert gcloud.region_for_bucket_location("") == ("us-east1", False)


def test_the_aws_spelling_of_a_region_is_not_a_region():
    """Close enough to be typed by mistake, far enough to match nothing."""
    assert gcloud.valid_region("us-east1") is True
    assert gcloud.valid_region("us-east-1") is False
    assert gcloud.valid_zone("us-east1-b") is True
    assert gcloud.valid_zone("us-east1") is False
    assert gcloud.region_of_zone("europe-west4-a") == "europe-west4"


def test_a_machine_type_says_its_size_in_sizes():
    """Somebody choosing between e2-highmem-16 and n2-highmem-32 is choosing
    between 128 GB and 256 GB, and the form should be where they find that out
    rather than a documentation page."""
    assert gcloud.machine_type_label("e2-highmem-16") == \
        "e2-highmem-16 · 16 vCPU · 128 GB RAM"
    assert gcloud.DEFAULT_MACHINE_TYPE == "e2-highmem-16"


def test_a_fraction_of_a_core_is_not_offered_as_a_core():
    """`e2-medium` and `e2-standard-4` both count their vCPUs the same way in
    their own naming, and they are not the same offer -- the first is a burst
    ceiling on a shared core. Nothing else in the name says so, which is why
    the label has to."""
    assert gcloud.machine_type_label("e2-medium") == \
        "e2-medium · 2 shared vCPU · 4 GB RAM"
    assert gcloud.machine_type_label("e2-standard-4") == \
        "e2-standard-4 · 4 vCPU · 16 GB RAM"
    shared = [one for one in gcloud.machine_types() if one["shared"]]
    assert [one["name"] for one in shared] == ["e2-medium"]


def test_there_is_something_small_enough_to_try_a_connection_on():
    """The default is sized for a 40-channel pyramid, which is the right
    default and the wrong thing to rent while checking that a bucket mounts
    and a tunnel opens."""
    offered = [one["name"] for one in gcloud.machine_types()]
    assert "e2-medium" in offered
    assert gcloud.DEFAULT_MACHINE_TYPE in offered


def test_the_shortlist_is_short_enough_to_read_to_the_end():
    """It carried sixteen rows once, two of which were 1 GB and 2 GB of RAM --
    a picker whose first entries are machines nobody working on imaging data
    should choose. A shortlist that has to be read to the end is not much
    better than the catalogue it stands in for, and the Custom box is what
    makes shortening it safe."""
    offered = [one["name"] for one in gcloud.machine_types()]
    assert len(offered) <= 10
    # One small, some general-purpose, some memory-heavy: the three reasons
    # anybody is on this form.
    assert any(name.startswith("e2-standard") for name in offered)
    assert any("highmem" in name for name in offered)


def test_a_machine_type_the_shortlist_never_heard_of_is_still_a_name():
    """The curated list is a shortlist and always will be -- Compute Engine has
    hundreds of types, and GPU machines, C3 and `custom-4-8192` are exactly the
    ones somebody who wants them already knows the name of. So the check is the
    SHAPE of a machine type, not membership of the list."""
    assert gcloud.valid_machine_type("c3-highmem-22")
    assert gcloud.valid_machine_type("custom-4-8192")
    assert gcloud.valid_machine_type("n2-custom-8-16384")
    # ...and a typo is still caught, here, rather than by `instances create`
    # four screens into provisioning.
    assert not gcloud.valid_machine_type("e2 highmem 16")
    assert not gcloud.valid_machine_type("E2-HIGHMEM-16")
    assert not gcloud.valid_machine_type("--flag")
    assert not gcloud.valid_machine_type("")


def test_a_vm_name_is_something_compute_engine_would_accept():
    assert gcloud.instance_name("My Lab GCP") == "plexora-my-lab-gcp"
    assert gcloud.instance_name("o2!!") == "plexora-o2"
    assert gcloud.instance_name("") == "plexora-vm"


# -- the reuse ladder --------------------------------------------------------


def _signed_in(runner, instance_status=None, labels=None):
    body = {"status": instance_status}
    if labels is not None:
        body["labels"] = labels
    described = (0, json.dumps(body), "") \
        if instance_status else (1, "", "ERROR: was not found")
    runner.answers = [
        ("auth list", (0, json.dumps([{"account": "aj@example.com"}]), "")),
        ("firewall-rules list", (0, json.dumps([
            {"direction": "INGRESS", "sourceRanges": [gcloud.IAP_RANGE],
             "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}]}]), "")),
        ("instances describe", described),
        # Before the general ssh answer, because the startup probe is also a
        # `compute ssh` and would otherwise be answered by it -- with the
        # wrong marker, which is a machine that never finishes starting up.
        ("PLEXORA_STARTUP_DONE", (0, "PLEXORA_STARTUP_DONE\n", "")),
        ("compute ssh", (0, "PLEXORA_SSH_OK\n", "")),
    ]


def test_a_running_vm_is_reused_rather_than_recreated(runner):
    _signed_in(runner, "RUNNING")
    said = []
    assert gcloud.ensure_instance(CFG, echo=said.append) == "reused"
    assert any("Reusing the VM plexora-gcp" in line for line in said)
    assert not any("instances create" in call for call in runner.argv)


def test_a_stopped_vm_is_started_rather_than_recreated(runner):
    _signed_in(runner, "TERMINATED")
    said = []
    gcloud.ensure_instance(CFG, echo=said.append)
    assert any("Starting the VM" in line for line in said)
    assert any("instances start plexora-gcp" in call for call in runner.argv)
    assert not any("instances create" in call for call in runner.argv)


def test_a_vm_that_does_not_exist_is_created_and_announced(runner):
    """Announced because the three rungs cost wildly different amounts:
    reusing is free, starting is a minute, and creating is a machine somebody
    is now paying for."""
    _signed_in(runner, None)
    said = []
    gcloud.ensure_instance(CFG, echo=said.append)
    assert any("Requesting a new e2-highmem-16 in us-east1-b" in line
               for line in said)
    created = next(call for call in runner.argv if "instances create" in call)
    # The shape of the whole thing: tagged so the deny rule covers it, OS
    # Login on, and a startup script that installs Cloud Storage FUSE at
    # first boot.
    assert "--tags plexora" in created
    assert "enable-oslogin=TRUE" in created
    assert "startup-script=" in created
    assert "--machine-type e2-highmem-16" in created
    assert "--boot-disk-size 50GB" in created


def test_a_new_vm_is_asked_for_at_the_spot_price(runner):
    """The same hardware at 60-91% off, on the condition that Google may take
    it back. For a long-running server that is a serious risk; here it is an
    interruption -- the data is in the bucket rather than on the machine.

    **STOP, not DELETE**, and that is the whole of why this is defensible as a
    default: a preempted VM is stopped, so the disk with `~/plexora-venv` on it
    survives and the reuse ladder starts the same machine again. Being
    reclaimed costs a reconnect rather than a rebuild."""
    _signed_in(runner, None)
    gcloud.ensure_instance(CFG, echo=lambda line: None)
    created = next(call for call in runner.argv if "instances create" in call)
    assert "--provisioning-model=SPOT" in created
    assert "--instance-termination-action=STOP" in created
    assert gcloud.DEFAULT_PROVISIONING == gcloud.PROVISIONING_SPOT


def test_a_standard_vm_carries_no_spot_flags_at_all(runner):
    """`--instance-termination-action` is refused by gcloud unless the machine
    is Spot or has a run duration, so this is not a flag that can be passed
    harmlessly to both."""
    _signed_in(runner, None)
    gcloud.ensure_instance({**CFG, "provisioning_model": "standard"},
                           echo=lambda line: None)
    created = next(call for call in runner.argv if "instances create" in call)
    assert "provisioning-model" not in created
    assert "termination-action" not in created


def test_no_spot_capacity_is_not_reported_as_no_capacity(runner):
    """A zone runs out of spot long before it runs out for anybody paying full
    price, so the likeliest fix on a spot request is not a different zone at
    all -- and sending somebody to try four zones for a machine that would
    have been created immediately at Standard is a bad half-hour."""
    _signed_in(runner, None)
    runner.answers.insert(0, ("instances create", (
        1, "", "ERROR: The zone does not have enough resources available")))
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(CFG, echo=lambda line: None)
    assert "Spot" in raised.value.message
    assert "Standard" in raised.value.message


def test_the_way_out_of_no_spot_capacity_is_attached_to_the_failure(runner):
    """A sentence ending "ask for a Standard one instead" describes an edit to
    the saved profile that the reader would otherwise have to go and make by
    hand, three pages into a form, having worked out which page. The key is
    what lets the page put a button there instead."""
    _signed_in(runner, None)
    runner.answers.insert(0, ("instances create", (
        1, "", "ERROR: The zone does not have enough resources available")))
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(CFG, echo=lambda line: None)
    assert raised.value.recovery == gcloud.RECOVERY_STANDARD


def test_the_same_words_about_capacity_offer_nothing_at_standard(runner):
    """The identical refusal means something else on a Standard request: the
    zone genuinely has no capacity, and "buy it outright" is what was already
    being done. Offering the button there would be offering to change a field
    to the value it already holds."""
    _signed_in(runner, None)
    runner.answers.insert(0, ("instances create", (
        1, "", "ERROR: The zone does not have enough resources available")))
    cfg = dict(CFG, provisioning_model=gcloud.PROVISIONING_STANDARD)
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(cfg, echo=lambda line: None)
    assert "Spot" not in raised.value.message
    assert raised.value.recovery == ""


def test_a_failure_with_no_single_fix_offers_no_button(runner):
    """Quota, a disabled API, a missing billing account: each has a fix, and
    none of them is one edit to this record. A recovery guessed for those
    would be a button that changed somebody's configuration on a hunch."""
    _signed_in(runner, None)
    runner.answers.insert(0, ("instances create", (
        1, "", "ERROR: Quota CPUS exceeded in region us-east1")))
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(CFG, echo=lambda line: None)
    assert "quota" in raised.value.message.lower()
    assert raised.value.recovery == ""


# -- a way out ---------------------------------------------------------------
#
# The failure these were written for: a VM created with `--no-address` on a
# default subnet, which boots, answers the tunnel, looks entirely healthy, and
# cannot reach Google's own apt repository. The startup script marked itself
# done, the mount chain waited out its full five minutes for a package that
# was never coming, and the connection failed with six hundred characters of
# shell and no clue in it.


def _private(runner, nats=None, google_access=False):
    """Signed in, no VM yet, and a network described as this test wants it."""
    _signed_in(runner, None)
    routers = [{"name": "r", "nats": nats,
                "region": "https://…/regions/us-east1"}] if nats else []
    runner.answers[2:2] = [
        ("routers list", (0, json.dumps(routers), "")),
        ("subnets describe", (0, json.dumps(
            {"privateIpGoogleAccess": google_access}), "")),
    ]


def test_a_vm_with_no_way_out_is_refused_before_it_is_created(runner):
    """The cheapest moment to find out is before the machine exists. After it
    exists there is a VM and a disk to be billed for, eight minutes of
    somebody watching a progress line, and a failure that looks like Plexora
    rather than like a subnet."""
    _private(runner)
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance({**CFG, "external_ip": False},
                               echo=lambda line: None)
    said = str(raised.value)
    assert "cannot reach the internet" in said
    assert "Cloud NAT" in said
    assert "routers nats create" in said       # the command that fixes it
    assert not any("instances create" in call for call in runner.argv)


def test_cloud_nat_is_a_way_out_and_is_taken_as_one(runner):
    """A project that already has NAT is a project where the whole point of
    `--no-address` holds, and Plexora should not be handing out addresses in
    it."""
    _private(runner, nats=[{"name": "nat-1"}])
    gcloud.ensure_instance({**CFG, "external_ip": False},
                           echo=lambda line: None)
    created = next(call for call in runner.argv if "instances create" in call)
    assert "--no-address" in created
    assert not any("add-access-config" in call for call in runner.argv)


def test_nat_in_another_region_is_not_a_way_out_of_this_one(runner):
    """And the region is matched here rather than by `--filter=region:(…)`:
    a filter key gcloud does not recognise matches nothing and says so only in
    a warning, which would report a project that HAS NAT as having none and
    refuse a connection for a reason that is not true."""
    _private(runner, nats=[{"name": "nat-1"}])
    runner.answers[2] = ("routers list", (0, json.dumps(
        [{"name": "r", "nats": [{"name": "nat-1"}],
          "region": "https://…/regions/europe-west4"}]), ""))
    with pytest.raises(gcloud.GcloudError):
        gcloud.ensure_instance({**CFG, "external_ip": False},
                               echo=lambda line: None)
    listed = next(call for call in runner.argv if "routers list" in call)
    assert "--filter" not in listed


def test_private_google_access_alone_is_allowed_and_said_to_be_partial(runner):
    """It reaches Google's apt repository and not PyPI, because PyPI is not
    Google. Enough to let somebody proceed, not enough to let them think it
    will work."""
    _private(runner, google_access=True)
    said = []
    gcloud.ensure_instance({**CFG, "external_ip": False}, echo=said.append)
    assert any("PyPI" in line for line in said)
    assert any("instances create" in call for call in runner.argv)


def test_the_door_is_shut_before_the_address_is_handed_out(runner):
    """Ordering, pinned: a machine that got its address first would spend the
    seconds in between answering the whole internet."""
    _signed_in(runner, None)
    gcloud.ensure_instance(CFG, echo=lambda line: None)
    deny = next(i for i, call in enumerate(runner.argv)
                if gcloud.DENY_RULE in call)
    create = next(i for i, call in enumerate(runner.argv)
                  if "instances create" in call)
    assert deny < create


def test_the_deny_rule_can_only_reach_machines_plexora_made(runner):
    """Scoped to the tag rather than to the network. The strongest thing this
    rule can do is cut a Plexora VM off from the internet; an untagged machine
    in the same project is untouched by it."""
    _signed_in(runner, None)
    gcloud.ensure_instance(CFG, echo=lambda line: None)
    rule = next(call for call in runner.argv if gcloud.DENY_RULE in call)
    assert "--target-tags=plexora" in rule
    assert "--direction=INGRESS" in rule
    assert "--action=deny" in rule
    # Egress is the entire point of the address, so no rule here may touch it.
    assert "EGRESS" not in " ".join(runner.argv)


def test_a_vm_from_before_this_rule_is_tagged_before_it_is_addressed(runner):
    """Reconnecting to a VM an earlier Plexora created has to repair it, or it
    fails the same way forever on a machine that looks perfectly healthy. Same
    ordering as a fresh create, for the same reason."""
    _signed_in(runner, "RUNNING")
    gcloud.ensure_instance(CFG, echo=lambda line: None)
    tag = next(i for i, call in enumerate(runner.argv) if "add-tags" in call)
    address = next(i for i, call in enumerate(runner.argv)
                   if "add-access-config" in call)
    assert tag < address


def test_a_stopped_vm_is_repaired_before_it_is_started(runner):
    """Compute Engine runs the startup script on every boot, so a VM given its
    address while still stopped comes up and installs what it was always meant
    to. Repaired after the start, it boots into the same failure one more time
    and has to be put right by the mount chain instead."""
    _signed_in(runner, "TERMINATED")
    gcloud.ensure_instance(CFG, echo=lambda line: None)
    address = next(i for i, call in enumerate(runner.argv)
                   if "add-access-config" in call)
    start = next(i for i, call in enumerate(runner.argv)
                 if "instances start" in call)
    assert address < start


def test_a_vm_that_already_has_both_is_left_alone(runner):
    _signed_in(runner, "RUNNING")
    runner.answers[2] = ("instances describe", (0, json.dumps(
        {"status": "RUNNING", "tags": {"items": ["plexora"]},
         "networkInterfaces": [{"accessConfigs": [{"natIP": "34.1.2.3"}]}]}),
        ""))
    gcloud.ensure_instance(CFG, echo=lambda line: None)
    assert not any("add-tags" in call for call in runner.argv)
    assert not any("add-access-config" in call for call in runner.argv)


def test_a_machine_the_user_runs_is_never_given_an_address(runner):
    """Adding a public address to somebody else's server is not a repair. The
    same rule as the stop switch and the idle timer: it is not ours."""
    _signed_in(runner, "RUNNING")
    gcloud.ensure_instance({**CFG, "vm_source": "existing"},
                           echo=lambda line: None)
    assert not any("add-access-config" in call for call in runner.argv)
    assert not any("add-tags" in call for call in runner.argv)
    assert not any(gcloud.DENY_RULE in call for call in runner.argv)


def test_a_record_written_before_this_field_repairs_itself(runner):
    """Absent means on, and this is the profile that makes it matter: every
    one saved before the field existed describes exactly the VM that cannot
    install anything."""
    assert gcloud.wants_external_ip({"project": "p"}) is True
    assert gcloud.wants_external_ip({"external_ip": False}) is False


# -- answering is not the same as ready --------------------------------------


def test_a_rented_vm_is_waited_for_twice_and_the_second_wait_is_the_script(
        runner):
    """sshd answers within seconds of boot; the startup script is still
    running `apt-get` minutes later. On the smallest machine this preset
    offers -- 1 GB of RAM and a fraction of a vCPU -- that apt install is most
    of what the machine has, and the session's own ssh arriving in the middle
    of it is a connection made to a host with nothing left to answer with.

    The mount chain waits for the same marker, but it cannot start waiting
    until ssh has succeeded, which is the wrong side of the door. This wait is
    on the side that retries."""
    _signed_in(runner, None)
    gcloud.ensure_instance(CFG, echo=lambda line: None)
    probes = [i for i, call in enumerate(runner.argv) if "compute ssh" in call]
    marker = next(i for i, call in enumerate(runner.argv)
                  if gcloud.STARTUP_MARK in call)
    assert len(probes) >= 2
    assert probes[0] < marker


def test_the_second_question_is_whether_it_finished_not_whether_it_worked(
        runner):
    """`test -f` on the marker, not `command -v gcsfuse`. A startup script
    that finished having installed nothing still answers this one -- and the
    mount chain is what deals with that, which it can only do once it has a
    connection to deal with it through."""
    assert f"test -f {gcloud.STARTUP_MARK}" in gcloud.STARTUP_PROBE
    assert "gcsfuse" not in gcloud.STARTUP_PROBE


def test_a_startup_script_that_never_finishes_is_not_a_failed_connection(
        runner, monkeypatch):
    """It means the script hung, or the VM predates Plexora writing a marker
    at all. Both are things the mount chain knows how to get past, so waiting
    forever -- or refusing -- would be worse than going in and trying."""
    monkeypatch.setattr(gcloud, "STARTUP_READY_TIMEOUT", -1)
    _signed_in(runner, "RUNNING")
    runner.answers.insert(0, ("PLEXORA_STARTUP_DONE", (0, "", "")))
    said = []
    assert gcloud.ensure_instance(CFG, echo=said.append) == "reused"
    assert any("Carrying on anyway" in line for line in said)


def test_a_machine_the_user_runs_is_not_waited_on_for_a_script_it_never_had(
        runner):
    """It has no startup script and never will -- Plexora does not install one
    on somebody else's server. Waiting ten minutes for its marker would be ten
    minutes of nothing."""
    _signed_in(runner, "RUNNING")
    gcloud.ensure_instance({**CFG, "vm_source": "existing"},
                           echo=lambda line: None)
    assert not any(gcloud.STARTUP_MARK in call for call in runner.argv)


def test_connecting_without_being_signed_in_says_which_button_to_press(runner):
    runner.answers = [("auth list", (0, "[]", ""))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(CFG, echo=lambda line: None)
    assert "gcloud auth login" in str(raised.value)


def test_a_quota_refusal_is_repeated_as_the_thing_to_do_about_it(runner):
    _signed_in(runner, None)
    runner.answers.insert(0, ("instances create",
                              (1, "", "ERROR: Quota CPUS exceeded")))
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(CFG, echo=lambda line: None)
    assert "quota" in str(raised.value)
    assert "smaller machine type" in str(raised.value)


def test_a_vm_that_never_answers_names_the_role_that_is_usually_missing(runner,
                                                                       monkeypatch):
    _signed_in(runner, "RUNNING")
    runner.answers.insert(0, ("compute ssh", (255, "", "connection refused")))
    clock = iter([0, 0, 0, 10_000, 10_000, 10_000])
    monkeypatch.setattr(gcloud, "_now", lambda: next(clock))
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(CFG, echo=lambda line: None)
    assert "IAP-secured Tunnel User" in str(raised.value)


def test_the_iap_firewall_rule_is_created_only_when_nothing_allows_it(runner):
    _signed_in(runner, "RUNNING")
    runner.answers[1] = ("firewall-rules list", (0, "[]", ""))
    gcloud.ensure_instance(CFG, echo=lambda line: None)
    created = next(call for call in runner.argv
                   if "firewall-rules create" in call)
    assert gcloud.FIREWALL_RULE in created
    assert "--rules=tcp:22" in created
    assert f"--source-ranges={gcloud.IAP_RANGE}" in created


def test_a_firewall_rule_that_cannot_be_created_is_a_hint_not_a_failure(runner):
    """A project with its own network rules has an administrator who decided
    them, and refusing to try the thing that very likely already works would be
    the wrong call to make on their behalf."""
    _signed_in(runner, "RUNNING")
    runner.answers[1] = ("firewall-rules list", (0, "[]", ""))
    runner.answers.insert(0, ("firewall-rules create",
                              (1, "", "ERROR: permission denied")))
    said = []
    assert gcloud.ensure_instance(CFG, echo=said.append) == "reused"
    assert any("gcloud compute firewall-rules create" in line for line in said)


# -- the way in --------------------------------------------------------------


def test_the_session_goes_through_iap_and_hands_ssh_its_own_flags():
    argv = gcloud.ssh_argv(CFG, command="echo hi", ssh_flags=["-o", "X=1"])
    assert argv[:3] == ["gcloud", "compute", "ssh"]
    assert "--tunnel-through-iap" in argv
    # Everything after `--` reaches the underlying ssh untouched, which is what
    # makes this a drop-in for a plain ssh everywhere downstream.
    assert argv[argv.index("--") + 1:] == ["-o", "X=1"]
    assert argv[argv.index("--command") + 1] == "echo hi"


# -- the mount ---------------------------------------------------------------


def test_the_prep_chain_mounts_verifies_and_then_builds_the_environment():
    line = gcloud.prepare_command_line(CFG)
    order = [line.index(part) for part in (
        "gcsfuse --implicit-dirs", "Verifying data access", "plexora-venv")]
    assert order == sorted(order)
    assert ('gcsfuse --implicit-dirs --temp-dir "$HOME/.plexora-gcsfuse-tmp" '
            'my-imaging-bucket "$HOME/plexora-data"') in line
    # `~` becomes $HOME rather than being quoted: quoting it would make a
    # literal directory called `~` in whatever the working directory was.
    assert "'~/plexora-data'" not in line


def test_writes_are_staged_somewhere_the_disk_size_accounts_for():
    """gcsfuse copies a whole object to local disk before it can write it, and
    left to itself it stages into `/tmp`. On an image where that is a tmpfs
    this would be staging into RAM, so a large write would end as an OOM kill
    rather than as a disk that filled -- and a staging area whose location is
    unspecified cannot be reasoned about when choosing a boot disk size, which
    is the whole basis of the 50 GB default."""
    line = gcloud.prepare_command_line(CFG)
    staging = '"$HOME/.plexora-gcsfuse-tmp"'
    assert f"--temp-dir {staging}" in line
    # Created before the mount that names it, not after.
    assert line.index("mkdir -p") < line.index("--temp-dir")
    assert staging in line[:line.index("--temp-dir")]


def test_the_boot_disk_is_sized_for_what_is_actually_on_it():
    """Not the images -- those are in the bucket -- and not the project, which
    for a data-node session is on the user's own machine. What is on it is the
    Debian image, the venv, pip's cache and the staging area above: about 5 GB.

    The disk is also the one thing that goes on billing after the VM stops, so
    the default is the small end of what fits rather than the comfortable end.
    """
    assert gcloud.DEFAULT_BOOT_DISK_GB == 20
    assert gcloud.DEFAULT_BOOT_DISK_GB > gcloud.MIN_BOOT_DISK_GB


def test_the_floor_is_low_enough_to_accept_the_default():
    """A minimum above the default would refuse the form's own starting value
    -- which is exactly what happened when the default came down to 20 and the
    floor was still the 30 Plexora used to impose on top of Google's. The real
    floor is the image: a boot disk may not be smaller than what it is built
    from, and the Debian cloud image is 10 GB."""
    assert gcloud.MIN_BOOT_DISK_GB <= gcloud.DEFAULT_BOOT_DISK_GB
    assert gcloud.MIN_BOOT_DISK_GB >= 10


def test_the_wait_for_the_startup_script_ends_when_the_script_does():
    """Five minutes is the ceiling and not the plan. The loop watches the
    marker the startup script writes, so a first connection stops waiting the
    moment there is nothing left to wait for -- rather than sitting out the
    whole window to discover that an install failed in its first thirty
    seconds, which is exactly what it did."""
    line = gcloud.prepare_command_line(CFG)
    wait = line[line.index("while"):line.index("; done")]
    assert f"[ ! -f {gcloud.STARTUP_MARK} ]" in wait
    assert f"$n -lt {gcloud.GCSFUSE_WAIT_TRIES}" in wait


def test_the_marker_records_the_outcome_rather_than_the_ending():
    """`touch` at the end of a script with no `set -e` proves only that the
    script reached the end. This one ran to completion on a VM that had failed
    to install anything at all."""
    script = gcloud.startup_script(30)
    assert "command -v gcsfuse" in script
    assert f"echo no-gcsfuse > {gcloud.STARTUP_MARK}" in script


def test_a_half_built_vm_is_given_the_whole_recipe_and_not_half_of_it():
    """The rented fallback used to be a bare `apt-get install gcsfuse`, which
    cannot work on the machine that needs it: a VM whose startup script failed
    has the repository listed and no key for it, so apt's package list has
    never mentioned gcsfuse. Both branches now repeat the whole recipe."""
    for source in ("plexora", "existing"):
        line = gcloud.prepare_command_line({**CFG, "vm_source": source})
        assert "sources.list.d/gcsfuse.list" in line
        assert "apt-get update -y && apt-get install -y gcsfuse" in line


def test_gcsfuse_that_never_arrives_says_why_rather_than_how():
    """What this replaces printed the whole six-hundred-character chain and
    left somebody to work out from it what had gone wrong."""
    line = gcloud.prepare_command_line(CFG)
    tail = line[line.index("; done"):]
    assert "could not be installed" in tail
    assert "no route to the internet" in tail
    # Spliced into a single-quoted shell string, so it must not contain one.
    assert "'" not in gcloud._NO_GCSFUSE


def test_the_repository_key_is_used_as_published_rather_than_converted():
    """Google publishes it ASCII-armoured and apt takes an armoured key in
    `signed-by` directly, so `gpg --dearmor` converted a file that already
    worked into a dependency on a program the Debian 13 image does not ship.

    What that cost was almost entirely silent: `gpg: not found`, no keyring,
    the repository rejected as unsigned, and four steps later the only visible
    symptom -- "E: Unable to locate package gcsfuse"."""
    def commands(text):
        """The script without its comments -- which discuss `gpg --dearmor`
        precisely because it must not be run."""
        return "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("#"))

    for text in (gcloud.STARTUP_SCRIPT, gcloud.prepare_command_line(CFG)):
        assert "gpg --dearmor -o" not in commands(text)
        assert "| gpg" not in commands(text)
        assert "signed-by=/usr/share/keyrings/cloud.google.asc" in text
    # And the key is fetched to exactly the file `signed-by` names.
    assert "-o /usr/share/keyrings/cloud.google.asc" in gcloud.STARTUP_SCRIPT
    assert "-o /usr/share/keyrings/cloud.google.asc" in \
        gcloud.prepare_command_line(CFG)


def test_the_vm_keeps_its_own_log_because_the_serial_console_stops_answering():
    """Compute Engine only serves `get-serial-port-output` for a **running**
    instance -- and a VM whose first boot failed is a VM Plexora then stops,
    so by the time anybody looks, the one place the reason was written has
    stopped answering. Twice during this preset's development the evidence was
    lost exactly that way."""
    script = gcloud.startup_script(30)
    assert f"tee -a {gcloud.STARTUP_LOG}" in script
    assert gcloud.LOG_PLACEHOLDER not in script
    assert gcloud.LOG_PLACEHOLDER not in gcloud.startup_script(0)


def test_a_failed_install_prints_what_the_machine_said():
    """The version this replaces threw the error away -- `>/dev/null 2>&1` --
    and then guessed at a cause in prose. A confident wrong guess is worse
    than none: it sends somebody to check a network that was fine."""
    line = gcloud.prepare_command_line(CFG)
    assert f"> {gcloud.INSTALL_LOG} 2>&1" in line
    assert "/dev/null 2>&1 || true" not in line
    assert f"tail -n 25 {gcloud.INSTALL_LOG}" in line
    assert f"tail -n 25 {gcloud.STARTUP_LOG}" in line


def test_the_diagnostic_redirection_is_the_way_round_that_prints():
    """`2>/dev/null >&2` sends stdout to /dev/null, because redirections are
    applied left to right and fd2 is already /dev/null by the time fd1 is
    pointed at it. Written that way it printed a header, a footer, and nothing
    in between."""
    line = gcloud.prepare_command_line(CFG)
    for log in (gcloud.INSTALL_LOG, gcloud.STARTUP_LOG):
        assert f"tail -n 25 {log} >&2 2>/dev/null" in line
        assert f"tail -n 25 {log} 2>/dev/null >&2" not in line


def test_the_failure_sentence_does_not_diagnose_what_it_cannot_see():
    """It offers the commonest cause and says to read the log first, rather
    than asserting a cause that was right once and wrong afterwards."""
    assert "read the log first" in gcloud._NO_GCSFUSE
    assert "'" not in gcloud._NO_GCSFUSE     # spliced into a quoted string


def test_the_smallest_machine_offered_has_somewhere_to_swap():
    """The smallest tier on the menu is a fraction of two cores, and a Debian
    cloud image has no swap at all -- so asking pip to resolve and unpack
    scipy, scikit-image and pyarrow on one can be an OOM kill with nothing in
    the log to say so. Two gigabytes of a 50 GB disk turns the smallest tier
    from a machine that might not finish installing into a slow one that
    does."""
    assert gcloud.SHARED_CORE & {
        entry["name"] for entry in gcloud.machine_types()}
    script = gcloud.startup_script(30)
    assert "mkswap /swapfile" in script
    assert script.index("swapon /swapfile") < script.index("apt-get install")


def test_the_image_ships_a_python_that_plexora_can_be_installed_on():
    """Debian 12 ships Python 3.11 and Plexora needs 3.12, so `pip install
    plexora` on a bookworm VM ends in "No matching distribution found for
    plexora" -- which reads as "this package does not exist" -- after
    absolutely everything else about the connection has worked. Trixie ships
    3.13."""
    assert gcloud.IMAGE_FAMILY == "debian-13"


def test_a_profile_is_not_held_to_the_image_the_old_default_wrote_into_it(
        runner):
    """Nothing in the form can set `image_family`, so a saved profile naming
    one is carrying a default rather than expressing a preference. Honouring
    it would mean every connection saved before the Debian 13 change kept
    rebuilding Debian 12 VMs and kept failing on Python 3.11, however many
    times somebody deleted the machine."""
    _signed_in(runner, None)
    gcloud.ensure_instance({**CFG, "image_family": "debian-12"},
                           echo=lambda line: None)
    created = next(call for call in runner.argv if "instances create" in call)
    assert "--image-family debian-13" in created
    # A family Plexora never shipped IS a choice, and is left alone.
    assert gcloud.image_family({"image_family": "ubuntu-2404-lts"}) \
        == "ubuntu-2404-lts"


def test_the_floor_is_the_one_the_package_actually_declares():
    """Pinned to pyproject.toml rather than trusted to stay in step with it:
    this module may not import `plexora`, so the number is written twice and
    this is what makes the second copy honest."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    floor = re.search(r'requires-python\s*=\s*"[^"]*>=\s*(\d+)\.(\d+)', text)
    assert floor, "pyproject.toml no longer declares a requires-python floor"
    assert gcloud.MIN_PYTHON == (int(floor.group(1)), int(floor.group(2)))


def test_a_python_too_old_is_one_sentence_and_not_a_wall_of_pip():
    """And the sentence names the fix, which depends on whose machine it is:
    a rented VM is thrown away and rebuilt, somebody's own machine is theirs
    to decide about."""
    rented = gcloud.prepare_command_line(CFG)
    assert "sys.version_info >= (3, 12)" in rented
    assert "Delete the VM in Settings" in rented
    byo = gcloud.prepare_command_line({**CFG, "vm_source": "existing"})
    assert "Install a newer Python on that machine" in byo
    # Checked on the install path only: a VM that already has Plexora on it
    # must not be refused by a check the install is what needed.
    assert rented.index("[ -x") < rented.index("sys.version_info")


def test_the_gcsfuse_suite_is_read_off_the_machine_not_written_in():
    """Naming one means the day IMAGE_FAMILY changes, apt is quietly pointed
    at a repository for a different Debian and the package stops existing."""
    from pathlib import Path

    source = Path(gcloud.__file__).read_text(encoding="utf-8")
    for codename in ("bookworm", "trixie", "bullseye"):
        assert f"gcsfuse-{codename}" not in source
    assert "VERSION_CODENAME" in gcloud.STARTUP_SCRIPT
    assert "VERSION_CODENAME" in gcloud.prepare_command_line(CFG)


def test_the_write_check_asks_for_writing_and_nothing_else():
    """`touch` creates the object and then sets its timestamps, and a bucket
    mount need not implement the second. Reporting that as "read-only" sends
    somebody to the IAM page to fix a permission they already have."""
    line = gcloud.prepare_command_line(CFG)
    check = line[line.index(".plexora-write-check") - 40:]
    assert "touch" not in check
    assert f": > " in line


def test_a_read_only_bucket_is_a_marker_rather_than_a_failed_mount():
    """Somebody else's published atlas is a perfectly ordinary thing to be
    given, and images open from one; what breaks is saving a figure into it."""
    line = gcloud.prepare_command_line(CFG)
    assert gcloud.MOUNT_READONLY_MARK in line
    # Write, then take it back: the check must leave nothing in the bucket.
    assert ": >" in line and "rm -f" in line


def test_the_readonly_marker_is_the_one_connect_py_watches_for():
    """The two files cannot import each other -- connect.py is required to stay
    loadable without the plexora package -- so the string is pinned instead."""
    from plexora import connect

    assert gcloud.MOUNT_READONLY_MARK == connect.MOUNT_READONLY_MARK


def test_a_bucket_name_that_could_carry_a_metacharacter_never_reaches_a_shell():
    for bad in ("my bucket", "a;rm -rf /", "$(id)", "Bucket"):
        with pytest.raises(gcloud.GcloudError):
            gcloud.prepare_command_line({**CFG, "bucket": bad})


def test_a_mount_path_is_a_plain_path_or_it_is_refused():
    assert gcloud.valid_mount_path("~/plexora-data") is True
    assert gcloud.valid_mount_path("/mnt/data") is True
    assert gcloud.valid_mount_path("~/data; rm -rf /") is False
    assert gcloud.valid_mount_path("~/../etc") is False
    assert gcloud.valid_mount_path("") is False


# -- what this module is not allowed to be able to do ------------------------


def test_deleting_the_vm_cannot_name_the_bucket(runner):
    """The guarantee the whole preset rests on, and it is structural rather
    than a promise: the lifecycle verbs are about a machine, and the argv they
    build has no room for a bucket in it."""
    runner.answers = [("instances describe", (0, json.dumps(
        {"status": "RUNNING", "labels": {"created-by": "plexora"}}), ""))]
    gcloud.stop_instance(CFG)
    gcloud.delete_instance(CFG)
    for call in runner.argv:
        assert "my-imaging-bucket" not in call
        assert "gs://" not in call
    assert "instances delete plexora-gcp" in runner.argv[-1]


def test_the_module_has_no_way_to_delete_storage_at_all():
    """Read as source rather than as behaviour, because the promise is about
    what cannot be reached from here -- including from a branch no test
    happens to take."""
    from pathlib import Path

    source = (Path(gcloud.__file__)).read_text(encoding="utf-8")
    for forbidden in ('"rm"', "storage rm", "buckets delete", "objects delete"):
        assert forbidden not in source


def test_a_saved_record_is_a_description_and_not_a_way_in():
    record = gcloud.profile(project="p", bucket="b", vm_name="plexora-x")
    assert record["version"] == 4
    text = json.dumps(record).lower()
    for forbidden in ("password", "token", "secret", "credential", "key"):
        assert forbidden not in text


def test_a_vm_plexora_did_not_make_cannot_be_deleted_through_it(runner):
    """The second structural promise, and the reason the check reads the
    machine rather than the record: a profile can be hand-edited, imported or
    simply wrong, and the label was written by the thing that created it."""
    runner.answers = [("instances describe",
                       (0, json.dumps({"status": "RUNNING"}), ""))]
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.delete_instance({**CFG, "vm_name": "somebody-elses-box"})
    assert "was not created by Plexora" in raised.value.message
    assert not any("instances delete" in call for call in runner.argv)


def test_the_delete_check_is_the_label_and_not_the_saved_field(runner):
    """Belt and braces, tested as such: a record claiming Plexora made this
    machine does not make it so."""
    runner.answers = [("instances describe", (0, json.dumps(
        {"status": "RUNNING", "labels": {"created-by": "somebody"}}), ""))]
    with pytest.raises(gcloud.GcloudError):
        gcloud.delete_instance({**CFG, "vm_source": "plexora"})


def test_a_machine_the_user_already_runs_is_never_created(runner):
    """Being asked to connect to `analysis-box` is not permission to build
    something called `analysis-box`."""
    _signed_in(runner, None)
    cfg = {**CFG, "vm_source": "existing", "vm_name": "analysis-box"}
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(cfg, echo=lambda line: None)
    assert "will not create one" in raised.value.message
    assert not any("instances create" in call for call in runner.argv)


def test_a_borrowed_machine_is_never_deleted_or_timed_out():
    """Deleting is forced off in `profile`, not left to the caller, so no path
    through the app can arrive at a record that would remove somebody else's
    server -- and the idle timer goes with it, because installing a shutdown
    timer on a machine Plexora did not build is not ours to do either.

    Stopping IS still allowed. That is a person answering a question about
    their own machine on the form; deleting one is not on the menu at any
    price."""
    record = gcloud.profile(vm_name="analysis-box", vm_source="existing",
                            on_exit="delete", idle_shutdown_minutes=45)
    assert record["on_exit"] == gcloud.EXIT_LEAVE
    assert record["idle_shutdown_minutes"] == 0
    assert gcloud.exit_action(record) == gcloud.EXIT_LEAVE
    kept = gcloud.profile(vm_name="analysis-box", vm_source="existing",
                          on_exit="stop")
    assert kept["on_exit"] == gcloud.EXIT_STOP


def test_a_rented_machine_stops_itself_by_default():
    """The conservative default, pinned: the mistake it prevents is a 16-core
    VM billing all weekend, and the mistake it causes is forty seconds."""
    record = gcloud.profile(vm_name="plexora-x")
    assert record["on_exit"] == gcloud.EXIT_STOP
    assert gcloud.exit_action(record) == gcloud.EXIT_STOP
    assert record["idle_shutdown_minutes"] == gcloud.IDLE_SHUTDOWN_MINUTES


def test_a_record_written_before_the_third_ending_existed_still_means_it():
    """v3 stored a `stop_vm_on_disconnect` boolean, which is the two-valued
    version of the same question. Reading it as such is what stops every
    profile saved before this change from silently switching to "leave
    running" -- which is the reading that costs money."""
    assert gcloud.exit_action({"stop_vm_on_disconnect": True}) \
        == gcloud.EXIT_STOP
    assert gcloud.exit_action({"stop_vm_on_disconnect": False}) \
        == gcloud.EXIT_LEAVE
    # And a record from before EITHER field existed gets the default, which is
    # the one that stops billing.
    assert gcloud.exit_action({}) == gcloud.EXIT_STOP
    # A record that lies about whose machine it is cannot buy a delete.
    assert gcloud.exit_action(
        {"on_exit": "delete", "vm_source": "existing"}) == gcloud.EXIT_LEAVE


def test_the_vm_is_given_a_way_to_switch_itself_off():
    """The only protection that survives the laptop dying, so it goes on at
    create time and its window is written into the script."""
    script = gcloud.startup_script(30)
    assert gcloud.IDLE_PLACEHOLDER not in script
    assert "IDLE_MINUTES=30" in script
    assert "plexora-idle-shutdown.timer" in script
    assert "shutdown -h now" in script


def test_switching_the_idle_timer_off_installs_nothing(runner):
    """Rather than a timer with an absurd window, so `systemctl list-timers`
    on the VM tells the truth about what is watching it."""
    script = gcloud.startup_script(0)
    assert "plexora-idle-shutdown" not in script
    # Still ends by recording what it managed to install, because that is
    # what the mount chain waits for -- switching the timer off must not
    # switch off the thing that tells a first connection it may stop waiting.
    assert script.rstrip().endswith("fi")
    assert f"echo ok > {gcloud.STARTUP_MARK}" in script


def test_the_idle_window_reaches_the_vm_that_is_being_created(runner):
    """Not just built correctly -- actually handed to Google. The script goes
    up as a file, so the file is what this reads back."""
    from pathlib import Path

    _signed_in(runner, None)
    gcloud.create_instance({**CFG, "idle_shutdown_minutes": 45},
                           echo=lambda line: None)
    created = next(call for call in runner.argv if "instances create" in call)
    path = created.split("startup-script=", 1)[1].split()[0]
    assert "IDLE_MINUTES=45" in Path(path).read_text(encoding="utf-8")


def test_no_gcloud_on_this_machine_is_said_once_and_plainly(monkeypatch):
    monkeypatch.setattr(gcloud, "_which", lambda name: None)
    assert gcloud.available() is False
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.ensure_instance(CFG, echo=lambda line: None)
    assert "cloud.google.com/sdk" in str(raised.value)


def test_a_gcloud_that_hangs_is_cut_off_rather_than_waited_on(monkeypatch):
    def hang(argv, timeout=None):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(gcloud, "_RUNNER", hang)
    monkeypatch.setattr(gcloud, "_which", lambda name: "/usr/bin/gcloud")
    with pytest.raises(gcloud.GcloudError) as raised:
        gcloud.projects()
    assert "did not answer" in str(raised.value)


# -- the routes the form asks while it is being filled in --------------------


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def _saved(name="gcp", **overrides):
    from plexora.server.models import remotes as remote_store

    record = dict(CFG)
    record.update(overrides)
    return remote_store.save(remote_store.Remote(
        name=name, target=record["vm_name"], local_node=False,
        remote_command="~/plexora-venv", data_dir="~/plexora-data",
        extra={"gcloud": record}))


def _owned(runner, labels={"created-by": "plexora"}):
    """Make `instances describe` answer with a machine Plexora made.

    Needed by anything that deletes, because the delete verb asks the instance
    who made it before it will touch it.
    """
    runner.answers = [("instances describe", (0, json.dumps(
        {"status": "RUNNING", "labels": labels}), ""))]


def test_the_form_is_told_plainly_that_nobody_is_signed_in(client, monkeypatch):
    """Not an error: "you have not signed in yet" is the ordinary first state
    of this form, and its answer is a button."""
    monkeypatch.setattr(gcloud, "available", lambda: True)
    monkeypatch.setattr(gcloud, "account", lambda: None)
    body = client.get("/settings/gcloud/status").get_json()
    assert body == {"installed": True, "account": None}


def test_no_cli_on_this_machine_is_reported_rather_than_raised(client,
                                                              monkeypatch):
    monkeypatch.setattr(gcloud, "available", lambda: False)
    body = client.get("/settings/gcloud/status").get_json()
    assert body["installed"] is False and body["account"] is None


def test_a_bucket_that_cannot_be_read_is_a_404_with_a_sentence(client,
                                                               monkeypatch):
    def refuse(project, name):
        raise gcloud.GcloudError(f"There is no bucket called gs://{name}.")

    monkeypatch.setattr(gcloud, "bucket", refuse)
    answer = client.get("/settings/gcloud/bucket",
                        query_string={"project": "p", "name": "nope"})
    assert answer.status_code == 404
    assert "no bucket called" in answer.get_json()["error"]


def test_a_google_refusal_is_never_a_500(client, monkeypatch):
    """A form cannot act on a traceback, and "the Compute Engine API is not
    enabled on this project" is the most useful thing anybody could be told."""
    def refuse():
        raise gcloud.GcloudError("The Compute Engine API is not enabled.")

    monkeypatch.setattr(gcloud, "projects", refuse)
    answer = client.get("/settings/gcloud/projects")
    assert answer.status_code == 400
    assert "not enabled" in answer.get_json()["error"]


def test_the_vm_routes_are_only_about_google_cloud_profiles(client,
                                                            plexora_data_root):
    from plexora.server.models import remotes as remote_store

    remote_store.save(remote_store.Remote(name="hpc", target="me@login.edu"))
    assert client.get("/settings/remotes/hpc/vm").status_code == 404
    assert client.post("/settings/remotes/hpc/vm/delete").status_code == 404
    assert client.get("/settings/remotes/nope/vm").status_code == 404


def test_a_deleted_vm_leaves_a_profile_that_can_be_connected_again(
        client, plexora_data_root, runner):
    """What was deleted is a rented machine; what the profile describes is
    which data to rent one FOR, so connecting again simply builds another."""
    from plexora.server.models import remotes as remote_store

    _saved()
    _owned(runner)
    answer = client.post("/settings/remotes/gcp/vm/delete")
    assert answer.status_code == 200
    assert remote_store.find("gcp") is not None
    assert remote_store.get("gcp").gcloud["bucket"] == "my-imaging-bucket"


def test_ending_a_vm_says_out_loud_what_survives_it(client, plexora_data_root,
                                                    runner):
    """The one thing somebody about to press Delete needs to be certain of."""
    _saved()
    _owned(runner)
    for path in ("/settings/remotes/gcp/vm/stop",
                 "/settings/remotes/gcp/vm/delete"):
        message = client.post(path).get_json()["message"]
        assert "never deletes storage" in message


def test_the_delete_button_refuses_a_machine_the_user_already_ran(
        client, plexora_data_root, runner):
    """Refused on the sentence here and on the label underneath, so a profile
    that lies about `vm_source` is refused anyway."""
    _saved(vm_source="existing", vm_name="analysis-box")
    _owned(runner, labels={})
    answer = client.post("/settings/remotes/gcp/vm/delete")
    assert answer.status_code == 400
    assert "will not delete it" in answer.get_json()["error"]
    assert not any("instances delete" in call for call in runner.argv)


def test_reconnecting_with_standard_changes_the_saved_profile(
        client, plexora_data_root, runner):
    """Not a one-shot override. A record saying Spot while the machine it
    describes was bought outright would be wrong on the Settings card, wrong
    in the form and wrong on the next create -- and the price is exactly the
    thing that must not quietly differ from what the record says."""
    from plexora.server.models import remotes as remote_store

    _saved(provisioning_model="spot")
    answer = client.post("/settings/remotes/gcp/vm/standard")
    assert answer.status_code == 200
    assert answer.get_json()["provisioning_model"] == "standard"
    assert remote_store.find("gcp").gcloud["provisioning_model"] == "standard"


def test_reconnecting_with_standard_changes_only_the_price(
        client, plexora_data_root, runner):
    """The point of the button is that everything else about the request was
    right: the same zone, the same machine, the same bucket, refused only
    because nobody had a spare one going cheap this minute."""
    from plexora.server.models import remotes as remote_store

    _saved(provisioning_model="spot")
    client.post("/settings/remotes/gcp/vm/standard")
    after = remote_store.find("gcp").gcloud
    assert after["zone"] == CFG["zone"]
    assert after["machine_type"] == CFG["machine_type"]
    assert after["bucket"] == CFG["bucket"]
    # And no machine was touched: this is called on a connection that already
    # failed, and the next connect is what talks to Compute Engine.
    assert runner.argv == []


def test_reconnecting_with_standard_is_refused_for_a_borrowed_machine(
        client, plexora_data_root, runner):
    """Plexora does not choose how somebody else's server was bought, and a
    Spot capacity refusal cannot happen on one: nothing here created it."""
    _saved(vm_source="existing", vm_name="analysis-box")
    answer = client.post("/settings/remotes/gcp/vm/standard")
    assert answer.status_code == 400
    assert "does not choose how it is bought" in answer.get_json()["error"]


def test_the_status_route_says_whose_machine_it_is(client, plexora_data_root,
                                                   runner):
    """What the page needs in order to offer Delete on a rented VM and
    withhold it on somebody's own server."""
    _saved()
    _owned(runner)
    body = client.get("/settings/remotes/gcp/vm").get_json()
    assert body["vm_source"] == "plexora"
    assert body["made_by_plexora"] is True


def test_no_route_can_reach_a_bucket_with_a_destructive_verb(client,
                                                             plexora_data_root,
                                                             runner):
    """The guarantee, checked at the layer a browser can actually reach."""
    _saved()
    client.post("/settings/remotes/gcp/vm/stop")
    client.post("/settings/remotes/gcp/vm/delete")
    for call in runner.argv:
        assert "storage" not in call
        assert "my-imaging-bucket" not in call


def test_the_vm_status_is_asked_for_rather_than_polled(client,
                                                       plexora_data_root,
                                                       runner):
    """A Compute Engine round trip per profile per second, for as long as
    anybody had the Settings page open, is not a status display."""
    _saved()
    runner.answers = [("instances describe",
                       (0, json.dumps({"status": "RUNNING"}), ""))]
    body = client.get("/settings/remotes/gcp/vm").get_json()
    assert body["status"] == "RUNNING"
    assert body["bucket"] == "my-imaging-bucket"

    listed = client.get("/settings/remotes").get_json()["remotes"]
    # The list every surface polls asks Google nothing at all.
    before = len(runner.calls)
    client.get("/settings/remotes")
    assert len(runner.calls) == before
    assert any(entry["name"] == "gcp" for entry in listed)


def test_a_vm_that_has_been_deleted_elsewhere_is_missing_not_broken(
        client, plexora_data_root, runner):
    _saved()
    runner.answers = [("instances describe", (1, "", "ERROR: was not found"))]
    assert client.get("/settings/remotes/gcp/vm").get_json()["status"] \
        == "missing"


def test_a_vm_can_be_found_by_name_without_knowing_its_zone(runner):
    """An instance name is only unique within a zone, so this searches the
    project. Rare enough to be worth the convenience of not making everybody
    name a zone they last thought about a year ago."""
    runner.answers = [("instances list", (0, json.dumps([
        {"name": "analysis-box", "zone": "https://…/zones/us-central1-a",
         "status": "TERMINATED", "machineType": "https://…/n2-highmem-32"},
        {"name": "other", "zone": "https://…/zones/us-east1-b",
         "status": "RUNNING", "machineType": "https://…/e2-medium"}]), ""))]
    assert gcloud.zone_of_instance("my-project", "analysis-box") \
        == "us-central1-a"
    assert gcloud.zone_of_instance("my-project", "nothing-called-this") == ""


def test_the_instance_list_is_the_whole_project_unless_a_zone_is_named(runner):
    """Where somebody's own machine lives has nothing to do with where their
    bucket is, and filtering by the bucket's zone hid exactly the machines the
    bring-your-own field exists to find."""
    runner.answers = [("instances list", (0, "[]", ""))]
    gcloud.instances("my-project")
    assert not any("--zones" in call for call in runner.argv)
    gcloud.instances("my-project", "us-east1-b")
    assert any("--zones us-east1-b" in call for call in runner.argv)


def test_the_vm_can_be_started_without_connecting_to_it(client,
                                                        plexora_data_root,
                                                        runner):
    """Connecting already starts a stopped machine, so this is not the only way
    up -- it is the way up for somebody who wants it warm before they need it.
    It matters more since stopping on disconnect became the default: stopped is
    now where one of these profiles rests."""
    _saved()
    answer = client.post("/settings/remotes/gcp/vm/start")
    assert answer.status_code == 200
    started = next(call for call in runner.argv if "instances start" in call)
    assert "instances start plexora-gcp" in started
    # Not waited on: Google has the instruction by the time this returns, and a
    # request that held a worker for a minute is a page that looks frozen.
    assert "--async" in started


def test_starting_a_vm_is_the_one_verb_that_takes_nothing_away(client,
                                                               plexora_data_root,
                                                               runner):
    """Stop and Delete end the live sessions first, because they are about to
    pull the floor out. Start is not, and ending somebody's connection in order
    to start a machine they are already connected to would be absurd."""
    _saved()
    client.post("/settings/remotes/gcp/vm/start")
    assert not any("instances stop" in call for call in runner.argv)
    assert not any("instances delete" in call for call in runner.argv)


def test_neither_ending_verb_waits_for_google_to_finish(client,
                                                        plexora_data_root,
                                                        runner):
    """The page has a status it can re-read. It does not need a request held
    open while Compute Engine gets round to it."""
    _saved()
    _owned(runner)
    client.post("/settings/remotes/gcp/vm/stop")
    stopped = next(call for call in runner.argv if "instances stop" in call)
    assert "--async" in stopped
