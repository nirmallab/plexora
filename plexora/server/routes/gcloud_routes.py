"""What the Google Cloud form asks while somebody is filling it in.

Seven read-only lookups and five verbs about one VM. All of them are thin: the
knowledge is in `plexora.gcloud`, and what is here is the translation from an
HTTP request into one of its calls and back -- including the rule that a
refusal from Google is a 400 or a 404 with a sentence in it, never a 500 with
a traceback. A form cannot act on a stack trace, and "the Compute Engine API
is not enabled on this project" is genuinely the most useful thing anybody
could be told at that moment.

**Nothing here handles a credential.** Signing in is `gcloud auth login`'s own
browser flow, started detached and never waited on; what comes back through
this process is an email address, which is the only part of it Plexora has any
use for. There is no token in any response below and no field for one in any
request.

**And nothing here can delete data.** The lifecycle verbs are about a
machine -- status, start, stop, delete -- and the module they call has no
storage-deletion capability at all. Deleting the VM a session rented is the
ordinary end of a session; the bucket it was reading is the thing the user
had before Plexora existed and still has afterwards.

**Nor a machine Plexora did not make.** `vm/delete` refuses a profile that
points at a VM the user already runs, and refuses it twice: once here on the
saved record, for the sentence, and once inside `gcloud.delete_instance` on
the label written to the instance itself, which is the check that still holds
when the record is wrong.
"""

import dataclasses

from flask import jsonify, request

from plexora import app


def _gcloud():
    """Imported per request, like every other model this route layer uses."""
    from plexora import gcloud

    return gcloud


def _failed(exc, status=400):
    """One GcloudError, as the sentence and the status a form can act on."""
    return jsonify(error=str(exc.message or exc), detail=exc.detail), status


@app.route('/settings/gcloud/status')
def gcloud_status():
    """Is the CLI here, and who is signed in.

    The first thing the form asks and the thing it goes back to while a sign-in
    is in flight. Both answers are ordinary: no CLI is a machine that needs one
    installed, and no account is somebody who has not signed in yet. Neither is
    an error, and reporting either as one would put a red box in front of the
    button that fixes it.
    """
    gcloud = _gcloud()
    installed = gcloud.available()
    return jsonify(installed=installed,
                   account=(gcloud.account() if installed else None))


@app.route('/settings/gcloud/auth', methods=['POST'])
def gcloud_auth():
    """Start the browser sign-in, and answer immediately.

    202 and a poll, for the same reason connecting is: what happens next is a
    person reading a Google consent screen, and a request that waited for that
    would pin a worker for however long they take to find their password.
    """
    gcloud = _gcloud()
    try:
        gcloud.begin_login()
    except gcloud.GcloudError as exc:
        return _failed(exc)
    return jsonify(started=True), 202


@app.route('/settings/gcloud/projects')
def gcloud_projects():
    gcloud = _gcloud()
    try:
        return jsonify(projects=gcloud.projects())
    except gcloud.GcloudError as exc:
        return _failed(exc)


@app.route('/settings/gcloud/buckets')
def gcloud_buckets():
    """Every bucket in one project, each with the region it implies.

    The region rides along because it is the answer to the question the form
    asks next. Compute belongs where the data already is, and a list that made
    somebody look each bucket up in the console would be asking them to do
    that join by hand -- which is exactly the step where the mistake that
    costs money in egress gets made.
    """
    gcloud = _gcloud()
    project = (request.args.get("project") or "").strip()
    if not project:
        return jsonify(error="Choose a project first."), 400
    try:
        return jsonify(buckets=gcloud.buckets(project))
    except gcloud.GcloudError as exc:
        return _failed(exc)


@app.route('/settings/gcloud/bucket')
def gcloud_bucket():
    """One bucket, checked -- before anything has been created or billed.

    A typed bucket name is the one field on this form that can be wrong in a
    way nothing else catches, and the cost of catching it late is a VM that
    exists, is running, and cannot see the data it was started for.
    """
    gcloud = _gcloud()
    project = (request.args.get("project") or "").strip()
    name = (request.args.get("name") or "").strip()
    if not project or not name:
        return jsonify(error="Choose a project and name a bucket."), 400
    try:
        return jsonify(bucket=gcloud.bucket(project, name))
    except gcloud.GcloudError as exc:
        return _failed(exc, 404)


@app.route('/settings/gcloud/instances')
def gcloud_instances():
    """The VMs already in a project, for somebody bringing their own.

    Only ever a convenience: the field it fills is a text box, and a VM in a
    zone this list did not cover is typed in and works exactly the same. What
    it saves is the tab-to-the-console-and-back that otherwise sits in the
    middle of filling this form in.
    """
    gcloud = _gcloud()
    project = (request.args.get("project") or "").strip()
    zone = (request.args.get("zone") or "").strip()
    if not project:
        return jsonify(error="Choose a project first."), 400
    if zone and not gcloud.valid_zone(zone):
        return jsonify(error=f"“{zone}” is not a Google Cloud zone."), 400
    try:
        return jsonify(instances=gcloud.instances(project, zone))
    except gcloud.GcloudError as exc:
        return _failed(exc)


@app.route('/settings/gcloud/zones')
def gcloud_zones():
    gcloud = _gcloud()
    project = (request.args.get("project") or "").strip()
    region = (request.args.get("region") or "").strip()
    if not project or not gcloud.valid_region(region):
        return jsonify(error="Choose a project and a region."), 400
    try:
        found = gcloud.zones(project, region)
    except gcloud.GcloudError as exc:
        return _failed(exc)
    return jsonify(zones=found, pick=(found[0] if found else ""))


# -- the VM a saved profile owns --------------------------------------------


def _profile(name):
    """The saved profile and its Google Cloud record, or a 404 response."""
    from plexora.server.models import remotes as remote_store

    remote = remote_store.find(name)
    if remote is None:
        return None, None, (jsonify(error=f"No saved server named “{name}”."),
                            404)
    record = remote.gcloud
    if not record:
        return None, None, (
            jsonify(error=f"“{name}” is not a Google Cloud connection."), 404)
    return remote, record, None


#: Said on every response that ends a VM, in these words, because it is the
#: one thing somebody about to press "Delete VM" needs to be sure of. The
#: guarantee is structural -- `plexora.gcloud` has no storage verb -- and this
#: is where it is said out loud.
BUCKET_UNTOUCHED = ("Your bucket and everything in it are untouched — Plexora "
                    "never deletes storage.")


@app.route('/settings/remotes/<name>/vm')
def gcloud_vm_status(name):
    """What this profile's VM is doing right now.

    On demand, never polled. The connection list is re-read every second while
    anything is happening, and putting a Compute Engine round trip in that loop
    would mean a gcloud subprocess per profile per second for as long as
    anybody had the Settings page open.
    """
    gcloud = _gcloud()
    _remote, record, refusal = _profile(name)
    if refusal:
        return refusal
    try:
        found = gcloud.instance(record.get("project"), record.get("zone"),
                                record.get("vm_name"))
    except gcloud.GcloudError as exc:
        return _failed(exc)
    return jsonify(vm=record.get("vm_name"),
                   zone=record.get("zone"),
                   machine_type=record.get("machine_type"),
                   bucket=record.get("bucket"),
                   # Whose machine it is, so the page can offer Delete on a
                   # rented one and withhold it on somebody's own server.
                   vm_source=record.get("vm_source") or gcloud.VM_PLEXORA,
                   made_by_plexora=gcloud.made_by_plexora(found or {}),
                   # "missing" rather than an error: a profile whose VM has
                   # been deleted is a perfectly good profile, and connecting
                   # it again simply creates another one.
                   status=(found or {}).get("status") or "missing")


def _end_sessions(name):
    """Stop both halves of this connection before touching the machine."""
    from plexora.server.models import remote_sessions
    from plexora.server.routes.settings_routes import _forget_node

    remote_sessions.stop(name, remote_sessions.KIND_VIEWER)
    remote_sessions.stop(name, remote_sessions.KIND_NODE)
    _forget_node(name)


@app.route('/settings/remotes/<name>/vm/start', methods=['POST'])
def gcloud_vm_start(name):
    """Start the VM without connecting to it.

    Connecting already starts a stopped machine, so this is not the only way
    up -- it is the way up for somebody who wants it warm before they need it,
    or who is about to do something on it that is not a Plexora session. It
    matters more than it used to: stopping on disconnect is the default now,
    so stopped is the ordinary resting state of one of these profiles, and a
    card that could only ever stop things was describing half a lifecycle.

    Sessions are deliberately NOT ended here. This is the one VM verb that
    does not take anything away.
    """
    gcloud = _gcloud()
    _remote, record, refusal = _profile(name)
    if refusal:
        return refusal
    try:
        gcloud.start_instance(record, block=False)
    except gcloud.GcloudError as exc:
        return _failed(exc)
    return jsonify(ok=True, status="STAGING", message=(
        f"Starting {record.get('vm_name')}. It takes about a minute."))


@app.route('/settings/remotes/<name>/vm/standard', methods=['POST'])
def gcloud_vm_standard(name):
    """Buy this profile's VM outright from now on, instead of at the Spot price.

    The one recovery `plexora.gcloud` hands out as a key rather than a
    sentence: a zone with no spare Spot capacity is not a broken configuration,
    and the fix is this single field. Everything else about the profile --
    zone, machine type, bucket, mount, ending -- is deliberately untouched, so
    that pressing the button retries exactly the request that was refused, at
    the price that is not being refused.

    **The saved profile really does change**, and that is the point rather than
    a side effect. A one-shot override would leave the record saying Spot while
    the machine it describes was Standard, and every surface reading that
    record -- the Settings card, the form, the next connection's create -- would
    be describing a VM that does not exist. The card says `standard` afterwards,
    so the change is visible where the price is.

    No session is stopped. This is called from a failed connection, where there
    is nothing left running to disturb, and it does not touch Compute Engine at
    all -- the next connect does that.
    """
    gcloud = _gcloud()
    from plexora.server.models import remotes as remote_store

    remote, record, refusal = _profile(name)
    if refusal:
        return refusal
    if record.get("vm_source") == gcloud.VM_EXISTING:
        return jsonify(error=(
            f"“{name}” connects to a VM you already run, so Plexora does not "
            f"choose how it is bought.")), 400
    was = record.get("provisioning_model") or gcloud.DEFAULT_PROVISIONING
    if was == gcloud.PROVISIONING_STANDARD:
        return jsonify(ok=True, provisioning_model=was, message=(
            f"“{name}” already asks for a Standard VM."))
    extra = dict(remote.extra or {})
    extra["gcloud"] = dict(record,
                           provisioning_model=gcloud.PROVISIONING_STANDARD)
    # `Remote` is frozen, and `replace` is what keeps every other field of it
    # exactly as it was rather than rebuilding one from the fields this route
    # happens to know about.
    remote_store.save(dataclasses.replace(remote, extra=extra))
    return jsonify(ok=True, provisioning_model=gcloud.PROVISIONING_STANDARD,
                   message=(f"“{name}” will ask for a Standard VM from now on. "
                            f"It costs full price and nobody reclaims it."))


@app.route('/settings/remotes/<name>/vm/stop', methods=['POST'])
def gcloud_vm_stop(name):
    """Stop the VM. The disk survives, and so does everything in the bucket.

    The middle of the three answers to "I am finished for now". A stopped VM
    costs only its disk, and starting it again is much faster than creating
    one because the environment the first connection built is still on it.
    """
    gcloud = _gcloud()
    _remote, record, refusal = _profile(name)
    if refusal:
        return refusal
    _end_sessions(name)
    try:
        # Not waited on: Google has the instruction by the time this returns,
        # and the page has a status it can re-read rather than a spinner it
        # would have to hold a worker for.
        gcloud.stop_instance(record, block=False)
    except gcloud.GcloudError as exc:
        return _failed(exc)
    return jsonify(ok=True, status="STOPPING", message=(
        f"Stopping {record.get('vm_name')}. {BUCKET_UNTOUCHED}"))


@app.route('/settings/remotes/<name>/vm/delete', methods=['POST'])
def gcloud_vm_delete(name):
    """Delete the VM and its boot disk. **Never the bucket.**

    The profile itself is deliberately kept. What was deleted is a rented
    machine; what the profile describes is which data to rent one for, and
    connecting again simply builds a new VM against the same bucket. Somebody
    who wants the profile gone as well presses Forget, which is a different
    button and says what it does.

    Refused outright for a profile pointing at a machine the user already
    runs. The check below is the polite one -- it reads the saved record and
    gives a sentence -- and `gcloud.delete_instance` makes the same check
    against the label on the instance itself, which is the one that holds even
    if this record is wrong.
    """
    gcloud = _gcloud()
    _remote, record, refusal = _profile(name)
    if refusal:
        return refusal
    if record.get("vm_source") == gcloud.VM_EXISTING:
        return jsonify(error=(
            f"“{record.get('vm_name')}” is a VM you already ran, not one "
            f"Plexora created, so Plexora will not delete it. You can stop it "
            f"here, or remove it in the Google Cloud console.")), 400
    _end_sessions(name)
    try:
        gcloud.delete_instance(record)
    except gcloud.GcloudError as exc:
        return _failed(exc)
    return jsonify(ok=True, status="missing", message=(
        f"Deleted {record.get('vm_name')}. {BUCKET_UNTOUCHED}"))
