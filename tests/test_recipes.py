"""Starting points for adding a server, and the promises they make.

Adding a remote server means answering questions about somebody else's
cluster. Those answers are properties of the SITE and are the same for everyone
who works there, so asking each person to discover them by failing is the wrong
shape. A recipe answers them in advance.

Two things make that safe rather than merely convenient, and both are pinned
here:

**A recipe cannot produce a profile the form could not.** It composes a body
and goes through the same save -- so in particular there is still nowhere in it
to put a password, which is the invariant the whole feature rests on.

**An untested preset says so.** Only HMS O2 and MGB ERISTwo are pinned to
observed behaviour (DEPLOYMENT.md quotes a real O2 session). Presenting a guess
with the same confidence as a verified fact is how somebody spends an afternoon
on a partition that never existed.
"""

import json

import pytest

import plexora
from plexora.server.models import recipes as recipe_store
from plexora.server.models import remotes as remote_store


@pytest.fixture
def client():
    return plexora.app.test_client()


# -- the catalogue -----------------------------------------------------------


def test_every_recipe_is_shaped_like_a_saved_profile():
    """`srun` carries the same three-valued meaning it has on `Remote`: None
    is "no scheduler", "" is "use srun with this site's defaults", and a string
    is arguments. A recipe that got that wrong would silently turn a login node
    into the machine serving tiles."""
    for recipe in recipe_store.all_recipes():
        assert recipe.id and recipe.label and recipe.blurb
        assert recipe.srun is None or isinstance(recipe.srun, str)
        assert isinstance(recipe.bind_node, bool)
        if recipe.flow:
            continue
        assert "{user}" in recipe.target_template or "{host}" in recipe.target_template


def test_a_recipe_that_asks_for_the_address_has_somewhere_to_put_it():
    for recipe in recipe_store.all_recipes():
        if recipe.flow:
            continue
        if "{host}" in recipe.target_template:
            assert "host" in recipe.ask, recipe.id
        else:
            assert "host" not in recipe.ask, recipe.id


def test_a_preset_with_its_own_flow_composes_its_own_address():
    """The two invariants above are about the ask vocabulary and the template
    that consumes it, and a flow recipe has neither -- its machine does not
    exist yet, so there is no address to ask for. What it owes instead is that
    it still produces one, because everything downstream of `compose` is the
    ordinary save that every other preset goes through."""
    for recipe in recipe_store.all_recipes():
        if not recipe.flow:
            continue
        assert recipe.target_template == "", recipe.id
        assert recipe.ask == (), recipe.id
        # And its catalogue rides with it, rather than costing a second route.
        assert recipe.to_dict()["extra"]["flow"] == recipe.flow


def test_the_sites_we_have_actually_connected_to_are_the_ones_marked_tested():
    """Everything else naming a real cluster is shaped from documentation. The
    badge is the whole point of the distinction, so it has to be true."""
    assert [r.id for r in recipe_store.all_recipes() if r.tested] == [
        "hms-o2", "mgb-eris"]
    assert [r.id for r in recipe_store.all_recipes() if r.unverified] == [
        "aws", "gcloud"]


def test_a_generic_shape_is_not_something_that_can_be_untested():
    """"Any Slurm cluster" asserts nothing about any particular machine -- the
    user supplies the address -- so a badge on it would be a warning about
    nothing, and would devalue the one on the presets that need it."""
    for recipe in recipe_store.all_recipes():
        if recipe.site:
            continue
        assert recipe.unverified is False, recipe.id
        assert "{host}" in recipe.target_template, recipe.id


def test_an_unverified_preset_says_so_in_its_own_words_too():
    """Not only as a flag the UI renders: somebody reading the notes on the
    form should be told there, beside the values they are about to accept."""
    for recipe in recipe_store.all_recipes():
        if not recipe.unverified:
            continue
        assert any("Untested" in note for note in recipe.notes), recipe.id


def test_o2_is_the_login_node_and_a_job(client):
    """The two facts that make an O2 connection work, and the two people get
    wrong: connect to the LOGIN node, and let Plexora ask the scheduler."""
    o2 = recipe_store.find("hms-o2")
    assert o2.target_template == "{user}@o2.hms.harvard.edu"
    assert o2.srun == "-p interactive -t 4:00:00 -c 16 --mem 128G"
    # Not bind_node: O2 allows the second hop into the compute node via
    # pam_slurm_adopt, and forwarding from the login node instead would bind
    # the port on an interface the whole cluster can reach.
    assert o2.bind_node is False


def test_the_mgb_preset_leads_with_the_vpn():
    """Off the MGB network, ssh to ERISTwo does not get refused -- it gets
    nothing, and the connection dies at the first step with nothing on the far
    side to have said why. That reads as a broken preset, so the note has to
    come before the form rather than after the failure."""
    eris = recipe_store.find("mgb-eris")
    assert eris.label == "MGB-ERIS"
    assert eris.target_template == "{user}@eris2n7.research.partners.org"
    assert "VPN" in eris.notes[0]
    # And not as a badge saying the opposite of what the note says: this one
    # has been connected to, so the warning it carries is about the network,
    # not about whether the values below it were guessed.
    assert eris.unverified is False


def test_the_generic_slurm_preset_says_a_partition_may_be_required():
    """Its `srun` is empty on purpose -- "this site's defaults are fine" is a
    real answer. On a site with no default partition it is not, and the job is
    refused before anything else happens. The preset cannot know which kind of
    site it is pointed at, so it says so instead of guessing."""
    slurm = next(r for r in recipe_store.all_recipes() if r.id == "slurm")
    assert slurm.srun == ""
    notes = " ".join(slurm.notes)
    assert "no default partition" in notes
    assert "-p" in notes


def test_no_recipe_offers_a_scheduler_plexora_cannot_drive():
    """Plexora's scheduler support IS srun. An `bsub` box that quietly did
    nothing would be worse than not offering one."""
    for recipe in recipe_store.all_recipes():
        for word in ("bsub", "qsub", "sbatch"):
            assert word not in (recipe.srun or "")
            assert word not in " ".join(recipe.notes)


# -- composing ---------------------------------------------------------------


def test_a_recipe_and_a_username_make_a_profile(plexora_data_root):
    body = recipe_store.compose("hms-o2", {"user": "aj", "name": "o2"})
    assert body["target"] == "aj@o2.hms.harvard.edu"
    assert body["use_srun"] is True
    assert body["srun"] == "-p interactive -t 4:00:00 -c 16 --mem 128G"


def test_the_knobs_a_person_turns_replace_rather_than_append():
    """Somebody who wants eight hours has not thereby said anything about the
    partition -- so the site's own arguments survive, with one value changed."""
    body = recipe_store.compose("hms-o2", {"user": "aj", "walltime": "8:00:00",
                                           "cores": "32", "memory": "64G"})
    assert body["srun"] == "-p interactive -t 8:00:00 -c 32 --mem 64G"


# -- what a job asks for by default ------------------------------------------


def test_a_job_asks_for_what_a_multiplexed_image_actually_needs():
    """Four hours, sixteen cores, 128 GB. A 40-channel pyramid is tens of
    gigabytes before anything is drawn, so a default that merely lets the
    process start is a default that gets killed halfway through an import."""
    assert recipe_store.DEFAULT_WALLTIME == "4:00:00"
    assert recipe_store.DEFAULT_CORES == "16"
    assert recipe_store.DEFAULT_MEMORY == "128G"
    assert recipe_store.DEFAULT_SRUN == "-p interactive -t 4:00:00 -c 16 --mem 128G"


def test_the_numbers_on_screen_are_the_numbers_that_are_sent():
    """The form fills its boxes from `defaults()` and the srun line is spliced
    from the same constants, so the two cannot drift into disagreeing."""
    shown = recipe_store.defaults()
    composed = recipe_store.compose("hms-o2", {"user": "aj"})["srun"]
    assert f"-t {shown['walltime']}" in composed
    assert f"-c {shown['cores']}" in composed
    assert f"--mem {shown['memory']}" in composed


def test_every_site_that_runs_a_job_asks_for_cores():
    """The request was for a cores box wherever the job is described, not only
    on the one preset somebody happened to be looking at."""
    for recipe in recipe_store.all_recipes():
        if recipe.srun is None:
            assert recipe_store.ASK_CORES not in recipe.ask, recipe.id
            continue
        assert recipe_store.ASK_CORES in recipe.ask, recipe.id
        assert recipe_store.ASK_WALLTIME in recipe.ask, recipe.id
        assert recipe_store.ASK_MEMORY in recipe.ask, recipe.id


# -- the advanced box --------------------------------------------------------


def test_the_advanced_box_holds_what_the_other_boxes_do_not():
    """A walltime box reading 4:00:00 above a line reading `-t 8:00:00` would
    be two answers to one question, and only one of them would be the one that
    ran. So the three managed flags are not in the line somebody edits."""
    o2 = recipe_store.find("hms-o2")
    assert o2.srun_extra == "-p interactive"
    for flag in ("-t", "-c", "--mem"):
        assert flag not in o2.srun_extra.split()
    # A host with no scheduler has no job line at all, and says so rather than
    # offering an empty box that would do nothing.
    assert recipe_store.find("ssh").srun_extra is None


def test_advanced_job_options_replace_the_site_line_and_keep_the_boxes():
    """What the Advanced box sends is the BASE; the three visible boxes splice
    on top of it, because the visible thing has to be the thing that runs."""
    body = recipe_store.compose("hms-o2", {
        "user": "aj", "srun": "-p gpu --gres=gpu:1",
        "walltime": "2:00:00", "cores": "8", "memory": "64G"})
    assert body["srun"] == "-p gpu --gres=gpu:1 -t 2:00:00 -c 8 --mem 64G"


def test_an_emptied_advanced_box_is_a_real_answer():
    """"No extra flags" is a thing somebody can mean, and is different from a
    form that never asked -- so it is membership, not truthiness."""
    body = recipe_store.compose("hms-o2", {"user": "aj", "srun": "",
                                           "walltime": "4:00:00"})
    assert body["srun"] == "-t 4:00:00"
    # Not sent at all: the site's own line stands.
    assert recipe_store.compose("hms-o2", {"user": "aj"})["srun"] \
        == recipe_store.DEFAULT_SRUN


def test_the_launch_command_can_be_fixed_before_the_first_attempt():
    """ssh not finding `plexora` is by a wide margin the commonest reason a
    connection fails, and it is the one thing a preset cannot know."""
    body = recipe_store.compose(
        "hms-o2", {"user": "aj",
                   "remote_command": "conda run -n imaging plexora"})
    assert body["remote_command"] == "conda run -n imaging plexora"
    assert recipe_store.compose("hms-o2", {"user": "aj"})["remote_command"] \
        == "plexora"


def test_the_advanced_box_still_has_nowhere_to_put_a_password():
    """A second way to write a profile must not be able to produce one the
    form could not -- the invariant the whole feature rests on."""
    body = recipe_store.compose("hms-o2", {
        "user": "aj", "srun": "-p interactive", "password": "hunter2",
        "remote_command": "plexora"})
    assert "password" not in body
    assert "hunter2" not in json.dumps(body)


def test_a_site_with_no_arguments_of_its_own_still_uses_the_scheduler():
    """"" is a real answer and a different one from None: it means this is a
    login node and the site's defaults will do."""
    body = recipe_store.compose("slurm", {"user": "aj", "host": "login.edu"})
    assert body["use_srun"] is True
    assert body["srun"] == ""
    assert body["target"] == "aj@login.edu"


def test_a_plain_ssh_host_is_not_wrapped_in_a_job():
    body = recipe_store.compose("ssh", {"user": "aj", "host": "workstation"})
    assert body["use_srun"] is False
    assert body["target"] == "aj@workstation"


def test_a_missing_username_is_refused_rather_than_guessed():
    """An ssh with no user takes the laptop's own login name, which on a
    cluster is somebody else's account or nobody's -- and fails as "Permission
    denied", the one message that sends people looking for the wrong problem."""
    with pytest.raises(ValueError):
        recipe_store.compose("hms-o2", {"user": ""})
    with pytest.raises(ValueError):
        recipe_store.compose("ssh", {"user": "aj", "host": ""})


def test_an_unknown_recipe_is_a_key_error_not_a_blank_profile():
    with pytest.raises(KeyError):
        recipe_store.compose("nope", {"user": "aj"})


# -- Google Cloud: the preset whose machine does not exist yet ----------------


GCLOUD_ANSWERS = {
    "name": "gcp",
    "project": "my-project",
    "bucket": "my-imaging-bucket",
    "bucket_location": "US-EAST1",
    "region": "us-east1",
    "zone": "us-east1-b",
}


def test_the_google_cloud_preset_composes_a_machine_and_the_data_it_is_for():
    """Both halves in one body. The target is a VM that does not exist yet, and
    the data directory IS the bucket's mount point -- which is the whole premise
    of the preset: the data is what the user has, and the machine is a thing
    Plexora rents to read it."""
    body = recipe_store.compose("gcloud", GCLOUD_ANSWERS)
    assert body["target"] == "plexora-gcp"
    assert body["use_srun"] is False
    assert body["data_dir"] == "~/plexora-data"
    assert body["gcloud"]["bucket"] == "my-imaging-bucket"
    assert body["gcloud"]["region"] == "us-east1"
    assert body["gcloud"]["vm_name"] == "plexora-gcp"
    assert body["gcloud"]["machine_type"] == "e2-highmem-16"


def test_a_machine_type_off_the_shortlist_is_accepted_on_its_shape():
    """The form offers a curated list and a box for a type that list does not
    name, because Compute Engine has hundreds and the interesting ones -- GPU
    machines, C3, `custom-4-8192` -- are always the ones somebody already knows
    the name of. Refusing anything not on our shortlist would make the box a
    lie."""
    body = recipe_store.compose(
        "gcloud", {**GCLOUD_ANSWERS, "machine_type": "custom-8-32768"})
    assert body["gcloud"]["machine_type"] == "custom-8-32768"


def test_a_machine_type_that_is_not_one_is_refused_here_and_not_by_google():
    """A typo in that box would otherwise surface as a Compute Engine error
    four screens into provisioning, after the firewall check and the create
    call, which is the worst place to learn you meant a dash."""
    with pytest.raises(ValueError) as raised:
        recipe_store.compose(
            "gcloud", {**GCLOUD_ANSWERS, "machine_type": "e2 highmem 16"})
    assert "machine type" in str(raised.value)


def test_the_vm_name_is_derived_so_nothing_has_to_remember_an_instance_id():
    """The reuse ladder looks the instance up by name every time, so there is no
    id to keep in step with anything -- and a name somebody typed with spaces or
    capitals in it is not a name Compute Engine accepts."""
    body = recipe_store.compose("gcloud", {**GCLOUD_ANSWERS, "name": "My Lab GCP"})
    assert body["gcloud"]["vm_name"] == "plexora-my-lab-gcp"
    assert body["target"] == body["gcloud"]["vm_name"]


def test_there_is_no_connecting_without_a_bucket():
    """Not a warning and not a default: a connection with no bucket would start
    a machine, bill somebody for it, and open a viewer onto an empty directory.
    The refusal says what to do about it."""
    answers = dict(GCLOUD_ANSWERS)
    answers.pop("bucket")
    with pytest.raises(ValueError) as raised:
        recipe_store.compose("gcloud", answers)
    assert "bucket" in str(raised.value)


def test_a_project_is_required_too():
    answers = dict(GCLOUD_ANSWERS)
    answers.pop("project")
    with pytest.raises(ValueError):
        recipe_store.compose("gcloud", answers)


def test_a_region_is_refused_in_the_spelling_that_is_not_googles():
    """`us-east-1` is AWS. It is close enough to be typed by mistake and far
    enough to match nothing, and the message says which is which."""
    with pytest.raises(ValueError) as raised:
        recipe_store.compose("gcloud", {**GCLOUD_ANSWERS, "region": "us-east-1"})
    assert "us-east1" in str(raised.value)


def test_a_zone_from_another_region_is_refused_rather_than_quietly_kept():
    """The pair decides where the VM lands, and a mismatch is the one shape of
    that answer where the data and the compute end up in different places
    without anybody having chosen it."""
    with pytest.raises(ValueError):
        recipe_store.compose("gcloud",
                             {**GCLOUD_ANSWERS, "zone": "europe-west4-a"})


def test_the_region_follows_the_bucket_when_the_form_did_not_say():
    body = recipe_store.compose("gcloud", {
        **GCLOUD_ANSWERS, "region": "", "bucket_location": "EUROPE-WEST4",
        "zone": "europe-west4-a"})
    assert body["gcloud"]["region"] == "europe-west4"


def test_a_bucket_name_that_could_carry_a_shell_metacharacter_is_refused():
    """It is spliced into a gcsfuse command line on the VM, so the set of
    characters it may contain is the set that cannot mean anything there."""
    for bad in ("my bucket", "my;rm -rf /", "MyBucket", "a$(id)b"):
        with pytest.raises(ValueError):
            recipe_store.compose("gcloud", {**GCLOUD_ANSWERS, "bucket": bad})


def test_the_google_cloud_preset_still_has_nowhere_to_put_a_password():
    """The invariant the whole feature rests on, checked on the branch that was
    added last -- a second compose path must not be able to produce a profile
    the form could not."""
    body = recipe_store.compose("gcloud", {
        **GCLOUD_ANSWERS, "password": "hunter2", "token": "hunter2",
        "service_account_key": "hunter2"})
    assert "hunter2" not in json.dumps(body)
    assert "password" not in json.dumps(body["gcloud"])


def test_the_preset_says_the_bucket_survives_the_vm():
    """On the form, before anything is created -- because the fear this answers
    is the one somebody has while deciding whether to press the button."""
    recipe = recipe_store.find("gcloud")
    notes = " ".join(recipe.notes)
    assert "never deletes" in notes or "never delete" in notes
    assert "Untested" in notes


def test_saving_the_google_cloud_preset_lands_the_record_under_extra(
        client, plexora_data_root):
    """`extra` is the seam that survives every round trip a profile makes, and
    the one the Settings form cannot silently drop -- which is why the record
    goes there rather than into a dozen optional columns."""
    answer = client.post("/settings/recipes/gcloud", json=GCLOUD_ANSWERS)
    assert answer.status_code == 200, answer.get_json()

    saved = remote_store.get("gcp")
    assert saved.gcloud["bucket"] == "my-imaging-bucket"
    assert saved.data_dir == "~/plexora-data"
    raw = json.loads(remote_store.remotes_path().read_text(encoding="utf-8"))
    assert raw["gcp"]["gcloud"]["vm_name"] == "plexora-gcp"


def test_a_later_edit_in_settings_keeps_the_google_cloud_record(
        client, plexora_data_root):
    """The regression this shape is prone to: the Settings form has no box for
    any of it, so a save that read the payload for these keys would erase them
    on the first edit of an address."""
    client.post("/settings/recipes/gcloud", json=GCLOUD_ANSWERS)
    answer = client.post("/settings/remotes", json={
        "name": "gcp", "target": "plexora-gcp",
        "remote_command": "~/plexora-venv"})
    assert answer.status_code == 200

    assert remote_store.get("gcp").gcloud["bucket"] == "my-imaging-bucket"


def test_a_google_cloud_profile_is_reported_to_the_page(client,
                                                        plexora_data_root):
    """The card draws the bucket, the region and the machine type off this --
    a VM name on its own says nothing about what the connection is for."""
    client.post("/settings/recipes/gcloud", json=GCLOUD_ANSWERS)
    listed = client.get("/settings/remotes").get_json()["remotes"]
    entry = next(item for item in listed if item["name"] == "gcp")
    assert entry["gcloud"]["bucket"] == "my-imaging-bucket"
    # And every other profile says None rather than an empty object, because
    # that is the flag the modal and the card branch on.
    client.post("/settings/recipes/ssh",
                json={"user": "aj", "host": "workstation", "name": "ws"})
    listed = client.get("/settings/remotes").get_json()["remotes"]
    plain = next(item for item in listed if item["name"] == "ws")
    assert plain["gcloud"] is None


# -- the routes --------------------------------------------------------------


def test_the_catalogue_is_served_rather_than_shipped_in_every_page(client):
    """connectionModal.js is loaded on every page including the viewer. It
    should not carry a catalogue of cluster documentation it uses on one page
    in a hundred."""
    answer = client.get("/settings/recipes")
    assert answer.status_code == 200
    listed = answer.get_json()["recipes"]
    assert [r["id"] for r in listed] == [r.id for r in recipe_store.all_recipes()]
    assert listed[0]["tested"] is True
    assert any(r["tested"] is False for r in listed)


def test_saving_from_a_recipe_writes_an_ordinary_profile(client, plexora_data_root):
    answer = client.post("/settings/recipes/hms-o2",
                         json={"user": "aj", "name": "o2"})
    assert answer.status_code == 200

    saved = remote_store.get("o2")
    assert saved.target == "aj@o2.hms.harvard.edu"
    assert saved.srun == "-p interactive -t 4:00:00 -c 16 --mem 128G"
    # An ordinary profile: editable in Settings afterwards like any other, and
    # written to the same file in the same shape.
    raw = json.loads(remote_store.remotes_path().read_text(encoding="utf-8"))
    assert raw["o2"]["target"] == "aj@o2.hms.harvard.edu"


def test_a_recipe_has_nowhere_to_put_a_password(client, plexora_data_root):
    """The invariant the whole feature rests on, checked at the one place a new
    way of writing a profile could have broken it."""
    client.post("/settings/recipes/hms-o2",
                json={"user": "aj", "name": "o2", "password": "hunter2",
                      "secret": "hunter2"})

    text = remote_store.remotes_path().read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert not hasattr(remote_store.get("o2"), "password")


def test_an_answer_the_recipe_needs_and_did_not_get_is_a_400(client,
                                                             plexora_data_root):
    answer = client.post("/settings/recipes/hms-o2", json={"name": "o2"})
    assert answer.status_code == 400
    assert "username" in answer.get_json()["error"]
    assert remote_store.find("o2") is None


def test_an_unknown_preset_is_a_404(client, plexora_data_root):
    assert client.post("/settings/recipes/nope",
                       json={"user": "aj"}).status_code == 404


def test_a_recipe_cannot_write_a_name_the_form_would_refuse(client,
                                                            plexora_data_root):
    """`_askpass` is a route, not a server, and a profile called that would
    shadow it."""
    for name in ("_askpass", "a/b"):
        answer = client.post("/settings/recipes/ssh",
                             json={"user": "aj", "host": "h", "name": name})
        assert answer.status_code == 400, name


# -- a machine the user already runs -----------------------------------------


@pytest.fixture
def project_vms(monkeypatch):
    """The instances a project has, without a project."""
    from plexora import gcloud

    found = [{"name": "analysis-box", "zone": "us-central1-a",
              "status": "TERMINATED", "machine_type": "n2-highmem-32"}]
    monkeypatch.setattr(gcloud, "instances", lambda project, zone="": found)
    return found


BYO = {**GCLOUD_ANSWERS, "vm_source": "existing", "vm_name": "analysis-box",
       "zone": ""}


def test_a_vm_the_user_already_runs_is_named_rather_than_derived(project_vms):
    """The whole point of the option: connect to THAT machine, not to one
    Plexora would have called something else."""
    body = recipe_store.compose("gcloud", BYO)
    assert body["gcloud"]["vm_name"] == "analysis-box"
    assert body["target"] == "analysis-box"
    assert body["gcloud"]["vm_source"] == "existing"


def test_naming_a_vm_is_enough_because_google_knows_where_it_is(project_vms):
    """Somebody who knows their VM is called `analysis-box` should not have to
    remember which zone they put it in eighteen months ago."""
    body = recipe_store.compose("gcloud", BYO)
    assert body["gcloud"]["zone"] == "us-central1-a"


def test_where_somebody_elses_machine_lives_is_a_fact_not_a_preference(
        project_vms):
    """The rest of this form reasons outwards from the data: the bucket picks
    the region, the region picks the zone. A machine that already exists
    inverts that, and refusing its zone would be refusing the only zone that
    can possibly be right."""
    body = recipe_store.compose("gcloud", BYO)
    # The bucket is in US-EAST1 and the VM is not, and that is allowed.
    assert body["gcloud"]["bucket_location"] == "US-EAST1"
    assert body["gcloud"]["region"] == "us-central1"


def test_a_rented_vm_still_has_to_be_in_the_region_that_was_chosen(project_vms):
    """The relaxation above is only for a machine Plexora did not place. One it
    is about to create has no reason to be anywhere but beside the data."""
    with pytest.raises(ValueError) as raised:
        recipe_store.compose("gcloud", {**GCLOUD_ANSWERS,
                                        "zone": "us-central1-a"})
    assert "not in us-east1" in str(raised.value)


def test_a_vm_that_is_not_there_is_said_before_anything_is_billed(project_vms):
    """And it names the two things that fix it, because a typo and a VM in a
    project this account cannot list look identical from here."""
    with pytest.raises(ValueError) as raised:
        recipe_store.compose("gcloud", {**BYO, "vm_name": "not-a-machine"})
    assert "could not find a VM" in str(raised.value)
    assert "say which zone it is in" in str(raised.value)


def test_bringing_a_machine_switches_off_what_is_not_ours_to_decide(
        project_vms):
    """Enforced in `gcloud.profile`, checked here at the layer the form
    actually reaches: nothing that would change somebody else's server
    survives being asked for.

    Stopping one is deliberately still allowed -- that is a person answering a
    question about their own machine -- and deleting one is not, at any
    price."""
    body = recipe_store.compose("gcloud", {**BYO,
                                           "on_exit": "delete",
                                           "idle_shutdown_minutes": "45",
                                           "external_ip": True})
    assert body["gcloud"]["on_exit"] == "leave"
    assert body["gcloud"]["idle_shutdown_minutes"] == 0
    kept = recipe_store.compose("gcloud", {**BYO, "on_exit": "stop"})
    assert kept["gcloud"]["on_exit"] == "stop"
    # Same rule, third switch: giving somebody else's server a public address
    # is not a repair, it is a change to their network.
    assert body["gcloud"]["external_ip"] is False


def test_a_rented_vm_is_given_a_way_out_unless_it_is_told_otherwise(
        project_vms):
    """Absent means on, and the risk it reads around is the expensive one: a
    VM with no route to the internet cannot install Cloud Storage FUSE or
    Plexora, so it cannot connect at all. Switching this off is only right on
    a network that already has Cloud NAT, which is a thing somebody knows
    about their own project and never a thing to assume from silence."""
    body = recipe_store.compose("gcloud", GCLOUD_ANSWERS)
    assert body["gcloud"]["external_ip"] is True
    off = recipe_store.compose("gcloud", {**GCLOUD_ANSWERS,
                                          "external_ip": False})
    assert off["gcloud"]["external_ip"] is False
