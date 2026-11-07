"""Google Cloud over its JSON APIs, with an access token the page holds.

`plexora.gcloud` does everything through the `gcloud` CLI, which a web page
cannot start. This module is the same verbs over Google's own JSON APIs --
Compute Engine, Cloud Storage, Resource Manager -- so the Google Cloud form
works on the website, where urllib goes through the page's `fetch`
(`plexora/browser/fetch.py`). `gcloud.backend()` chooses between the two: this
one when the request carries a token, and always in the browser; the CLI
otherwise, so a desktop with gcloud installed is unchanged.

**The token is the page's, and it is never stored.** The page signs in with
Google (`services/googleAuth.js`: an access token for about an hour, no
refresh token) and sends the token with each Google Cloud request in
`TOKEN_HEADER`. A route binds it for that request only (`bind`/`unbind`, a
context variable), every call below sends it to Google in an Authorization
header, and nothing writes it anywhere. When it lapses, Google answers 401
and the form asks the page to sign in again.

**The scopes are the narrowest that cover the form** (`SCOPES`):
`compute` (zones, instances, firewall rules, the VM lifecycle),
`devstorage.read_only` (buckets, a bucket's contents), and
`cloudplatformprojects.readonly` (the project list -- Resource Manager accepts
it, so `cloud-platform` is never asked for), plus `openid email` to say who is
signed in.

**No SSH, so a different VM.** The desktop reaches its VM through IAP with
`gcloud compute ssh`, mounts the bucket and runs Plexora over that
connection. A web page has no ssh, so the VM does it all itself: its startup
script (`NODE_STARTUP_SCRIPT`) installs gcsfuse and Plexora, mounts the bucket
and runs `plexora node serve --app --tls self-signed` as a service, then
writes its state and its certificate's fingerprint as guest attributes, which
the page reads through the Compute API (it cannot reach a self-signed node
itself). The page then hands the user to `https://<ip>:<port>/app/` -- the
node's own origin, so no CORS and no local-network prompt, and one
certificate warning, which the fingerprint lets them check. The node's token
lives in the VM's own metadata (`plexora-node-token`), generated here when the
VM is made; Plexora keeps no copy.

The same promises as the CLI backend: **no storage-deletion verb**, and a VM
is deleted only when it carries the `created-by=plexora` label.

Standalone-loadable like `gcloud.py`: stdlib only, plus that module. One seam,
`_HTTP`, is where every request goes, so tests drive the whole of this on
canned API answers (tests/test_gcloud_rest.py).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from plexora import gcloud as cli
from plexora.gcloud import GcloudError

#: The request header the page sends the access token in.
TOKEN_HEADER = "X-Plexora-Google-Token"

#: Points every API below at one stand-in (tests/browser/mock_google.py):
#: `<base>/compute/v1`, `<base>/storage/v1`, `<base>/v1/projects`,
#: `<base>/v1/userinfo`. Unset means Google's own hosts.
API_ENV = "PLEXORA_GOOGLE_API"

#: What the page asks Google for. See the module docstring for why each.
SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/compute",
    "https://www.googleapis.com/auth/devstorage.read_only",
    "https://www.googleapis.com/auth/cloudplatformprojects.readonly",
)

#: A read-only lookup. The page waits on it, so it is short.
QUERY_TIMEOUT = 60
#: Waiting for one Compute Engine operation (a firewall rule, a machine type).
OPERATION_TIMEOUT = 180

#: The node a web VM runs: its port (above 1024, so it needs no root), the
#: network tag its ingress rule is scoped to, and that rule's name.
NODE_PORT = 8443
NODE_TAG = "plexora-node"
NODE_RULE = "plexora-allow-node-https"
#: Below `gcloud.DENY_PRIORITY`, so it wins over the deny-everything rule for
#: this one port and nothing else; the default an administrator's rule gets.
NODE_RULE_PRIORITY = 1000

#: The VM's own metadata keys. The token is the node's; the page reads it back
#: from here to make the hand-over link.
META_TOKEN = "plexora-node-token"
META_PORT = "plexora-node-port"
META_BUCKET = "plexora-bucket"
META_PACKAGE = "plexora-package"
META_IDLE = "plexora-idle-minutes"
#: Where the VM's startup script writes what it is doing (guest attributes).
GUEST_NAMESPACE = "plexora"

_TOKEN: contextvars.ContextVar = contextvars.ContextVar("plexora_google_token", default="")


def _default_http(method, url, token, body=None, timeout=QUERY_TIMEOUT):
    """`(status, bytes)` for one request to a Google API. Never raises for an
    HTTP status; a server that cannot be reached is a GcloudError."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 -- a body that will not read is no body
            raw = b""
        return int(exc.code), raw
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GcloudError("Google Cloud could not be reached. Check this "
                          "computer's connection and try again.", str(exc)) from None


#: The seam: rebound by tests with canned answers, like `gcloud._RUNNER`.
_HTTP = _default_http
_now = time.monotonic
_sleep = time.sleep


# -- the token ------------------------------------------------------------------


def token():
    """The access token bound to this request, or ""."""
    return _TOKEN.get()


def bind(value):
    """Bind `value` as this request's token; returns what `unbind` takes."""
    return _TOKEN.set(str(value or "").strip())


def unbind(handle):
    _TOKEN.reset(handle)


@contextlib.contextmanager
def signed_in(value):
    """`with signed_in(token): ...` -- the calls inside send `token`."""
    handle = bind(value)
    try:
        yield
    finally:
        unbind(handle)


def in_browser():
    """Python in the website's worker, where no CLI can ever run."""
    return sys.platform == "emscripten"


# -- the APIs -------------------------------------------------------------------


def _base():
    return (os.environ.get(API_ENV) or "").strip().rstrip("/")


def compute_api():
    base = _base()
    return f"{base}/compute/v1" if base else "https://compute.googleapis.com/compute/v1"


def storage_api():
    base = _base()
    return f"{base}/storage/v1" if base else "https://storage.googleapis.com/storage/v1"


def projects_api():
    base = _base()
    return f"{base}/v1/projects" if base else "https://cloudresourcemanager.googleapis.com/v1/projects"


def userinfo_api():
    base = _base()
    return f"{base}/v1/userinfo" if base else "https://openidconnect.googleapis.com/v1/userinfo"


def _q(part):
    return urllib.parse.quote(str(part), safe="")


#: The text a 401 carries, in any of the shapes Google words it.
_NEEDS_LOGIN = ("401", "unauthenticated", "invalid_token", "invalid authentication credentials",
                "sign in with google")


def needs_login(text):
    """Whether an error's detail means "sign in again" (either backend's words)."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _NEEDS_LOGIN) or cli.needs_login(text)


def _error(status, raw, what, recovery=""):
    """One refused request as a GcloudError: `what` for the person, Google's
    own words as the detail (they name the API to enable, the role to ask for)."""
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        reasons = " ".join(str(one.get("reason") or "") for one in error.get("errors") or ()
                           if isinstance(one, dict))
        detail = " ".join(part for part in (str(status), str(error.get("status") or ""),
                                            str(error.get("message") or ""), reasons) if part)
    elif isinstance(error, str):
        detail = f"{status} {error} {body.get('error_description') or ''}".strip()
    else:
        detail = f"{status} {(raw or b'')[:300].decode('utf-8', 'replace')}".strip()
    return GcloudError(what, detail, recovery=recovery, reauth=(status == 401))


def _call(method, url, what, body=None, missing=..., timeout=QUERY_TIMEOUT):
    """One request, as the JSON it answered. `missing` is returned for a 404
    when given (a VM that does not exist is an answer, not an error)."""
    held = token()
    if not held:
        raise GcloudError("Sign in with Google to continue.",
                          "401 this request carries no Google access token", reauth=True)
    if method == "POST" and body is None:
        # Google's front end answers 411 to a POST with no length.
        body = {}
    status, raw = _HTTP(method, url, held, body, timeout)
    if status == 404 and missing is not ...:
        return missing
    if status >= 400 or status == 0:
        raise _error(status, raw, what)
    try:
        return json.loads(raw or b"null")
    except (ValueError, TypeError):
        raise GcloudError(what, "Google answered with something that is not JSON.") from None


def _pages(url, what, params=None, limit=20):
    """Every page of a list call, as the list of page bodies."""
    params = dict(params or {})
    out = []
    for _ in range(limit):
        query = urllib.parse.urlencode(params)
        page = _call("GET", f"{url}?{query}" if query else url, what) or {}
        out.append(page)
        following = page.get("nextPageToken") if isinstance(page, dict) else None
        if not following:
            break
        params["pageToken"] = following
    return out


# -- who is signed in -------------------------------------------------------------


def available():
    """Always: nothing to install. Whether anybody is signed in is `health`'s."""
    return True


def executable():
    return None


def health():
    """The form's first page, in the CLI backend's shape plus `backend`.

    No token is the ordinary first state (the page's Sign in button). A token
    Google refuses with a 401 is `expired`; any other failure is a `problem`,
    in words, because offline is not a reason to send somebody to a consent
    screen."""
    out = {"installed": True, "account": None, "problem": None, "expired": False,
           "backend": "rest"}
    if not token():
        return out
    try:
        info = _call("GET", userinfo_api(), "Could not read who is signed in to Google.") or {}
    except GcloudError as exc:
        if exc.reauth:
            out["expired"] = True
        else:
            out["problem"] = "\n".join(part for part in (exc.message, exc.detail) if part)
        return out
    out["account"] = str(info.get("email") or info.get("sub") or "your Google account")
    return out


def account():
    return health().get("account")


def begin_login():
    raise GcloudError("On the website the sign-in happens in the page: press Sign in "
                      "with Google.")


# -- what you have --------------------------------------------------------------


def projects():
    """Every active project this account can use (Resource Manager)."""
    out = []
    for page in _pages(projects_api(), "Could not list your Google Cloud projects.",
                       {"filter": "lifecycleState:ACTIVE", "pageSize": "500"}):
        for entry in (page or {}).get("projects") or ():
            project_id = (entry or {}).get("projectId")
            if project_id:
                out.append({"id": str(project_id), "name": str(entry.get("name") or project_id)})
    return sorted(out, key=lambda item: item["id"])


def buckets(project):
    """Every bucket in `project`, each with the region it implies."""
    out = []
    for page in _pages(f"{storage_api()}/b", f"Could not list the buckets in “{project}”.",
                       {"project": project, "maxResults": "1000"}):
        for entry in (page or {}).get("items") or ():
            if (entry or {}).get("name"):
                out.append(cli._bucket_view(entry))
    return sorted(out, key=lambda item: item["name"])


def _bucket_name(text):
    name, folder = cli.split_bucket_url(text)
    if not cli.valid_bucket_name(name):
        raise GcloudError(
            f"“{name}” is not a Cloud Storage bucket name. Bucket names are "
            "lower-case letters, digits, dashes, underscores and dots.")
    return name, folder


def bucket(project, name):
    """One bucket, checked, as `gcloud.bucket` checks it: described, and when
    the description is refused, whether its objects can be listed anyway (a
    public dataset grants objects, not metadata)."""
    text, _folder = _bucket_name(name)
    described = None
    try:
        described = _call("GET", f"{storage_api()}/b/{_q(text)}",
                          f"Could not read the bucket gs://{text}.", missing=None)
    except GcloudError as exc:
        if exc.reauth:
            raise
        detail = (exc.detail or "").lower()
        if "403" in detail or "permission" in detail or "denied" in detail:
            if _readable_anyway(text):
                return cli._bucket_view({"name": text}, public=True)
            raise GcloudError(
                f"This account cannot read gs://{text}. Ask whoever owns the "
                "bucket for Storage Object Viewer on it.", exc.detail) from None
        raise
    if not described:
        raise GcloudError(f"There is no bucket called gs://{text} in “{project}”. "
                          "Check the name, or pick one from the list.")
    return cli._bucket_view(described)


def _readable_anyway(name):
    try:
        _call("GET", f"{storage_api()}/b/{_q(name)}/o?maxResults=1&fields=items(name)",
              "Could not list the bucket.")
    except GcloudError:
        return False
    return True


def objects(project, name, prefix="", page_token=""):
    """One page of what is in a bucket, one folder level at a time.

    `{bucket, prefix, folders, files, next}`: `folders` are the prefixes one
    level down (each ending in "/"), `files` the objects at this level with
    their size, `next` the page token for more ("" at the end). A folder in
    `name` (`bucket/folder`) is where the listing starts when `prefix` is not
    given."""
    text, folder = _bucket_name(name)
    if prefix is None or prefix == "":
        prefix = f"{folder}/" if folder else ""
    prefix = str(prefix).lstrip("/")
    params = {"delimiter": "/", "maxResults": "200",
              "fields": "prefixes,items(name,size,updated),nextPageToken"}
    if prefix:
        params["prefix"] = prefix
    if page_token:
        params["pageToken"] = page_token
    try:
        got = _call("GET", f"{storage_api()}/b/{_q(text)}/o?{urllib.parse.urlencode(params)}",
                    f"Could not list gs://{text}/{prefix}.", missing=None)
    except GcloudError as exc:
        detail = (exc.detail or "").lower()
        if not exc.reauth and ("403" in detail or "permission" in detail):
            raise GcloudError(f"This account cannot list gs://{text}. Ask whoever owns the "
                              "bucket for Storage Object Viewer on it.", exc.detail) from None
        raise
    if got is None:
        raise GcloudError(f"There is no bucket called gs://{text}.")
    files = [{"name": str(item["name"]), "size": int(item.get("size") or 0),
              "updated": str(item.get("updated") or "")}
             for item in got.get("items") or () if item.get("name") and item["name"] != prefix]
    return {"bucket": text, "prefix": prefix, "folders": [str(one) for one in got.get("prefixes") or ()],
            "files": files, "next": str(got.get("nextPageToken") or "")}


# -- where to put a VM ----------------------------------------------------------


def zones(project, region):
    """The zones of `region` that are up."""
    out = []
    for page in _pages(f"{compute_api()}/projects/{_q(project)}/zones",
                       f"Could not list the zones in {region}."):
        for entry in (page or {}).get("items") or ():
            if not (entry or {}).get("name"):
                continue
            if str(entry.get("region") or "").rsplit("/", 1)[-1] != region:
                continue
            if str(entry.get("status") or "UP").upper() != "UP":
                continue
            out.append(str(entry["name"]))
    return sorted(out)


def pick_zone(project, region):
    try:
        found = zones(project, region)
    except GcloudError as exc:
        if exc.reauth:
            raise
        refusal = cli.zone_refusal(project, exc)
        if refusal is not None:
            raise refusal from None
        return ""
    return found[0] if found else ""


# -- the VMs --------------------------------------------------------------------


def _instance_url(project, zone, name):
    return f"{compute_api()}/projects/{_q(project)}/zones/{_q(zone)}/instances/{_q(name)}"


def _row(entry, zone=""):
    return {"name": str(entry["name"]),
            "zone": str(entry.get("zone") or zone).rsplit("/", 1)[-1],
            "status": str(entry.get("status") or "").upper(),
            "machine_type": str(entry.get("machineType") or "").rsplit("/", 1)[-1]}


def instances(project, zone=""):
    """Every VM in a project, or in one zone of it, sorted by zone and name."""
    out = []
    what = "Could not list the VMs in this project."
    if zone:
        for page in _pages(f"{compute_api()}/projects/{_q(project)}/zones/{_q(zone)}/instances", what):
            out += [_row(entry, zone) for entry in (page or {}).get("items") or ()
                    if isinstance(entry, dict) and entry.get("name")]
    else:
        for page in _pages(f"{compute_api()}/projects/{_q(project)}/aggregated/instances", what,
                           {"returnPartialSuccess": "true"}):
            for scope, held in ((page or {}).get("items") or {}).items():
                for entry in (held or {}).get("instances") or ():
                    if isinstance(entry, dict) and entry.get("name"):
                        out.append(_row(entry, scope.rsplit("/", 1)[-1]))
    out.sort(key=lambda one: (one["zone"], one["name"]))
    return out


def zone_of_instance(project, name):
    for entry in instances(project):
        if entry.get("name") == name:
            return entry.get("zone") or ""
    return ""


def _described(project, zone, name):
    """The instance resource as Compute Engine has it, or None."""
    return _call("GET", _instance_url(project, zone, name),
                 f"Could not check whether the VM “{name}” exists.", missing=None)


def _metadata(described):
    items = ((described or {}).get("metadata") or {}).get("items") or ()
    return {str(item.get("key")): str(item.get("value") or "") for item in items
            if isinstance(item, dict) and item.get("key")}


def _view(described):
    """The CLI backend's `instance()` shape, from the resource."""
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
            "machine_type": str(described.get("machineType") or "").rsplit("/", 1)[-1],
            "labels": dict(labels) if isinstance(labels, dict) else {},
            "scopes": scopes,
            "tags": [str(tag) for tag in tags],
            "external_ip": external}


def instance(project, zone, name):
    """What is true about one VM, or None when it does not exist."""
    described = _described(project, zone, name)
    return None if not described else _view(described)


def _wait(operation, project, what, timeout=OPERATION_TIMEOUT):
    """Wait for a zonal or global operation to be DONE; raise its error."""
    operation = operation or {}
    deadline = _now() + timeout
    while str(operation.get("status") or "").upper() != "DONE":
        if _now() > deadline:
            raise GcloudError(what, f"Google had not finished after {timeout:g}s.")
        zone = str(operation.get("zone") or "").rsplit("/", 1)[-1]
        where = f"zones/{_q(zone)}" if zone else "global"
        operation = _call("POST", f"{compute_api()}/projects/{_q(project)}/{where}/operations/"
                                  f"{_q(operation.get('name') or '')}/wait", what) or {}
    failure = (operation.get("error") or {}).get("errors") or ()
    if failure:
        detail = " ".join(f"{one.get('code') or ''} {one.get('message') or ''}".strip()
                          for one in failure if isinstance(one, dict))
        raise GcloudError(what, detail)
    return operation


def _verb(cfg, verb, what, block):
    project = cli._cfg(cfg, "project")
    operation = _call("POST", _instance_url(project, cli._cfg(cfg, "zone"),
                                            cli._cfg(cfg, "vm_name")) + "/" + verb, what)
    if block:
        _wait(operation, project, what, timeout=cli.START_TIMEOUT)
    return True


def start_instance(cfg, block=True):
    """Start a stopped VM (`resume` for a suspended one)."""
    name = cli._cfg(cfg, "vm_name")
    return _verb(cfg, "start", f"Could not start the VM “{name}”.", block)


def resume_instance(cfg, block=True):
    name = cli._cfg(cfg, "vm_name")
    return _verb(cfg, "resume", f"Could not resume the VM “{name}”.", block)


def stop_instance(cfg, block=True):
    """Stop the VM. The disk survives, and so does everything in the bucket."""
    name = cli._cfg(cfg, "vm_name")
    return _verb(cfg, "stop", f"Could not stop the VM “{name}”.", block)


def delete_instance(cfg, block=True):
    """Delete a VM **Plexora created**, and its boot disk. Never the bucket.

    The label is read off the instance first, as the CLI backend reads it."""
    project = cli._cfg(cfg, "project")
    zone = cli._cfg(cfg, "zone")
    name = cli._cfg(cfg, "vm_name")
    described = instance(project, zone, name)
    if described is None:
        raise GcloudError(f"There is no VM called “{name}” in {zone} to delete. It may "
                          "already be gone.")
    if not cli.made_by_plexora(described):
        raise GcloudError(
            f"“{name}” was not created by Plexora, so Plexora will not delete it. "
            f"Machines you already ran are yours to remove, in the Google Cloud console.")
    what = f"Could not delete the VM “{name}”."
    operation = _call("DELETE", _instance_url(project, zone, name), what)
    if block:
        _wait(operation, project, what, timeout=cli.START_TIMEOUT)
    return True


def set_machine_type(cfg, machine_type):
    """Resize a stopped VM: another machine type, the same disk and data.

    Compute Engine only changes the type of a machine that is not running,
    so a running one is a sentence rather than a stop nobody asked for."""
    project = cli._cfg(cfg, "project")
    zone = cli._cfg(cfg, "zone")
    name = cli._cfg(cfg, "vm_name")
    if not cli.valid_machine_type(machine_type):
        raise GcloudError(f"“{machine_type}” is not a Compute Engine machine type.")
    found = instance(project, zone, name)
    if found is None:
        raise GcloudError(f"There is no VM called “{name}” in {zone}.")
    if found["status"] not in ("TERMINATED", "STOPPED"):
        raise GcloudError(f"Stop “{name}” before changing its size: Compute Engine only "
                          "resizes a machine that is not running.")
    what = f"Could not change “{name}” to {machine_type}."
    operation = _call("POST", _instance_url(project, zone, name) + "/setMachineType", what,
                      body={"machineType": f"zones/{zone}/machineTypes/{machine_type}"})
    _wait(operation, project, what)
    return True


# -- the web node VM ---------------------------------------------------------------

#: The node VM's startup script. Compute Engine runs it as root on every boot.
#: It does over no connection what the desktop's ssh session does: install
#: gcsfuse and Plexora, mount the bucket, and run the node -- as a service, as
#: an unprivileged `plexora` user, with the token from the VM's metadata in an
#: environment file only root can read (never on a command line). What it is
#: doing goes to guest attributes (`plexora/state`, `plexora/fingerprint`),
#: which the page reads through the Compute API.
NODE_STARTUP_SCRIPT = r"""#!/bin/bash
exec > >(tee -a /var/log/plexora-startup.log) 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
MD=http://metadata.google.internal/computeMetadata/v1/instance
attr() { curl -sf -H "Metadata-Flavor: Google" "$MD/attributes/$1"; }
say() { curl -sf -X PUT --data "$2" -H "Metadata-Flavor: Google" \
  "$MD/guest-attributes/plexora/$1" >/dev/null || true; }
say state installing

if ! swapon --show=NAME --noheadings | grep -q .; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! command -v gcsfuse >/dev/null 2>&1; then
  CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    -o /usr/share/keyrings/cloud.google.asc
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc]" \
       "https://packages.cloud.google.com/apt gcsfuse-${CODENAME} main" \
    > /etc/apt/sources.list.d/gcsfuse.list
  apt-get update -y
  apt-get install -y gcsfuse python3-venv python3-pip fuse openssl
fi
command -v gcsfuse >/dev/null 2>&1 || { say state failed-install; exit 1; }

id -u plexora >/dev/null 2>&1 || useradd --system --create-home \
  --home-dir /home/plexora --shell /usr/sbin/nologin plexora
HOMEDIR=/home/plexora
DATA=$HOMEDIR/plexora-data
mkdir -p "$DATA" "$HOMEDIR/.gcsfuse-tmp" "$HOMEDIR/plexora-home"
chown plexora:plexora "$DATA" "$HOMEDIR/.gcsfuse-tmp" "$HOMEDIR/plexora-home"

BUCKET=$(attr plexora-bucket)
NAME=${BUCKET%%/*}
FOLDER=""
case "$BUCKET" in */*) FOLDER=${BUCKET#*/};; esac
if [ -n "$NAME" ] && ! mountpoint -q "$DATA"; then
  ONLY=""
  [ -n "$FOLDER" ] && ONLY="--only-dir=$FOLDER"
  runuser -u plexora -- gcsfuse --implicit-dirs --temp-dir="$HOMEDIR/.gcsfuse-tmp" \
    $ONLY "$NAME" "$DATA" || { say state failed-mount; exit 1; }
fi

VENV=/opt/plexora-venv
PACKAGE=$(attr plexora-package || echo plexora)
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade "$PACKAGE" || [ -x "$VENV/bin/plexora" ] \
  || { say state failed-install; exit 1; }

PORT=$(attr plexora-node-port || echo __NODE_PORT__)
umask 077
printf 'PLEXORA_NODE_TOKEN=%s\nPLEXORA_DATA_PATH=%s\n' "$(attr plexora-node-token)" \
  "$HOMEDIR/plexora-home" > /etc/plexora-node.env
chmod 600 /etc/plexora-node.env
umask 022
cat > /etc/systemd/system/plexora-node.service <<UNIT
[Unit]
Description=Plexora web node
After=network-online.target

[Service]
User=plexora
EnvironmentFile=/etc/plexora-node.env
WorkingDirectory=$HOMEDIR
ExecStart=$VENV/bin/plexora node serve --host 0.0.0.0 --port $PORT --app --tls self-signed --dynamic --node-id $(hostname)
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

IDLE=$(attr plexora-idle-minutes || echo 30)
if [ "${IDLE:-0}" -gt 0 ]; then
  cat > /usr/local/sbin/plexora-idle-shutdown <<IDLESCRIPT
#!/bin/sh
# Shut this VM down once nobody has used the node for $IDLE minutes: the last
# line of defence against a bill, and it runs here, where the bill is.
STAMP=/var/lib/plexora-last-active
if ss -Htn state established "( sport = :$PORT )" | grep -q .; then date +%s > \$STAMP; exit 0; fi
[ -f \$STAMP ] || { date +%s > \$STAMP; exit 0; }
idle=\$(( (\$(date +%s) - \$(cat \$STAMP)) / 60 ))
[ "\$idle" -ge $IDLE ] && /sbin/shutdown -h now
exit 0
IDLESCRIPT
  chmod 0755 /usr/local/sbin/plexora-idle-shutdown
  date +%s > /var/lib/plexora-last-active
  printf '[Unit]\nDescription=Plexora idle check\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/plexora-idle-shutdown\n' \
    > /etc/systemd/system/plexora-idle-shutdown.service
  printf '[Unit]\nDescription=Plexora idle check\n[Timer]\nOnBootSec=5min\nOnUnitActiveSec=5min\n[Install]\nWantedBy=timers.target\n' \
    > /etc/systemd/system/plexora-idle-shutdown.timer
fi

systemctl daemon-reload
systemctl enable plexora-node
systemctl restart plexora-node
[ "${IDLE:-0}" -gt 0 ] && systemctl enable --now plexora-idle-shutdown.timer
say state starting
CODE=000
for i in $(seq 1 120); do
  CODE=$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:$PORT/app/")
  [ "$CODE" != "000" ] && break
  sleep 5
done
CERT="$HOMEDIR/plexora-home/node-tls/cert.pem"
if [ "$CODE" = "200" ] && [ -f "$CERT" ]; then
  say fingerprint "$(openssl x509 -noout -fingerprint -sha256 -in "$CERT" | cut -d= -f2)"
  say state ready
elif [ "$CODE" = "404" ]; then
  say state failed-noapp
else
  say state failed-start
fi
"""


def node_startup_script(port=NODE_PORT):
    return NODE_STARTUP_SCRIPT.replace("__NODE_PORT__", str(int(port)))


def _package():
    """What pip installs on the VM: this Plexora's release, unpinned for a
    development build (which PyPI does not have)."""
    try:
        from plexora import __version__
    except ImportError:
        return "plexora"
    text = str(__version__)
    if any(mark in text for mark in ("dev", "+", "a", "b", "rc")):
        return "plexora"
    return f"plexora=={text}"


def _node_metadata(cfg, node_token, port):
    idle = cli._cfg(cfg, "idle_shutdown_minutes", cli.IDLE_SHUTDOWN_MINUTES)
    try:
        idle = int(idle)
    except (TypeError, ValueError):
        idle = cli.IDLE_SHUTDOWN_MINUTES
    if idle and idle < cli.MIN_IDLE_SHUTDOWN_MINUTES:
        idle = cli.MIN_IDLE_SHUTDOWN_MINUTES
    return {
        "enable-oslogin": "TRUE",
        "enable-guest-attributes": "TRUE",
        "startup-script": node_startup_script(port),
        META_TOKEN: node_token,
        META_PORT: str(port),
        META_BUCKET: str(cli._cfg(cfg, "bucket")),
        META_PACKAGE: _package(),
        META_IDLE: str(idle),
    }


def node_instance_body(cfg, node_token, port=NODE_PORT):
    """The `instances.insert` body for a web node VM: what
    `gcloud.create_instance` asks for, plus the node's metadata and tag."""
    zone = cli._cfg(cfg, "zone")
    machine = cli._cfg(cfg, "machine_type") or cli.DEFAULT_MACHINE_TYPE
    disk = int(cli._cfg(cfg, "boot_disk_gb", cli.DEFAULT_BOOT_DISK_GB) or cli.DEFAULT_BOOT_DISK_GB)
    family = cli.image_family(cfg)
    image_project = cli._cfg(cfg, "image_project") or cli.IMAGE_PROJECT
    body = {
        "name": cli._cfg(cfg, "vm_name"),
        "machineType": f"zones/{zone}/machineTypes/{machine}",
        "disks": [{"boot": True, "autoDelete": True, "initializeParams": {
            "sourceImage": f"projects/{image_project}/global/images/family/{family}",
            "diskSizeGb": str(disk),
            "diskType": f"zones/{zone}/diskTypes/pd-balanced"}}],
        "networkInterfaces": [{"network": "global/networks/default",
                               "accessConfigs": [{"name": "external-nat",
                                                  "type": "ONE_TO_ONE_NAT"}]}],
        "metadata": {"items": [{"key": key, "value": value} for key, value
                               in _node_metadata(cfg, node_token, port).items()]},
        "tags": {"items": [cli.NET_TAG, NODE_TAG]},
        "serviceAccounts": [{
            "email": str(cli._cfg(cfg, "service_account") or "").strip() or "default",
            "scopes": ["https://www.googleapis.com/auth/devstorage.read_write",
                       "https://www.googleapis.com/auth/logging.write"]}],
        "labels": {cli.OWNER_LABEL: cli.OWNER_VALUE},
    }
    if cli._cfg(cfg, "provisioning_model", cli.DEFAULT_PROVISIONING) == cli.PROVISIONING_SPOT:
        body["scheduling"] = {"provisioningModel": "SPOT", "instanceTerminationAction": "STOP",
                              "automaticRestart": False, "onHostMaintenance": "TERMINATE"}
    return body


def _firewall_names(project):
    names = {}
    for page in _pages(f"{compute_api()}/projects/{_q(project)}/global/firewalls",
                       "Could not list firewall rules."):
        for rule in (page or {}).get("items") or ():
            if isinstance(rule, dict) and rule.get("name"):
                names[str(rule["name"])] = rule
    return names


def _insert_firewall(project, rule, what):
    operation = _call("POST", f"{compute_api()}/projects/{_q(project)}/global/firewalls", what,
                      body=rule)
    _wait(operation, project, what)


def ensure_node_firewall(project, port=NODE_PORT):
    """The two rules a web node needs, created before the VM that needs them.

    `gcloud.DENY_RULE` (tag `plexora`, priority 65000) closes the machine to
    everything, as on the desktop; `NODE_RULE` (tag `plexora-node`, priority
    1000) opens the node's one port. The node itself refuses every data
    request without its token. Returns notes for anything best effort that
    could not be done; raises when the node's port cannot be opened, because
    the page could then never reach it."""
    notes = []
    existing = _firewall_names(project)
    if cli.DENY_RULE not in existing:
        try:
            _insert_firewall(project, {
                "name": cli.DENY_RULE, "network": "global/networks/default",
                "direction": "INGRESS", "priority": cli.DENY_PRIORITY,
                "denied": [{"IPProtocol": "all"}], "sourceRanges": ["0.0.0.0/0"],
                "targetTags": [cli.NET_TAG],
                "description": "Plexora: no inbound except what another Plexora rule allows"},
                "Could not add the rule that closes Plexora's VMs to the internet.")
        except GcloudError as exc:
            if exc.reauth:
                raise
            notes.append("Plexora could not add its deny-all rule, so this VM is protected "
                         "only by the project's own firewall rules.")
    rule = existing.get(NODE_RULE)
    opened = False
    if rule:
        for allowed in rule.get("allowed") or ():
            if str(port) in [str(one) for one in allowed.get("ports") or ()]:
                opened = True
    if not opened:
        try:
            _insert_firewall(project, {
                "name": NODE_RULE if not rule else f"{NODE_RULE}-{port}",
                "network": "global/networks/default",
                "direction": "INGRESS", "priority": NODE_RULE_PRIORITY,
                "allowed": [{"IPProtocol": "tcp", "ports": [str(port)]}],
                "sourceRanges": ["0.0.0.0/0"], "targetTags": [NODE_TAG],
                "description": "Plexora: the web node's HTTPS port (every data request "
                               "needs the node's token)"},
                "Could not open the node's port.")
        except GcloudError as exc:
            if exc.reauth:
                raise
            raise GcloudError(
                f"Plexora could not open port {port} for the VM's node, so this browser "
                f"could never reach it. Ask whoever administers “{project}” for the "
                f"Compute Security Admin role, or to add an ingress rule allowing tcp:{port} "
                f"to instances tagged {NODE_TAG}.", exc.detail) from None
    return notes


def ensure_node(cfg, block=False):
    """Have this profile's VM running a web node: created, started or reused.

    `{action, notes}`, with `action` one of created, started, resumed, reused,
    converted (a VM the desktop made, given the node's metadata and tag while
    stopped, then started) or waiting (Compute Engine is mid-transition).
    Nothing here waits for the boot: the page polls `node_status`, because a
    request held for a minute is a frozen page.

    Only a VM Plexora rents: a machine the user already runs would need
    software installed on it, which only an SSH session (the desktop's) can
    do with their say-so."""
    project = cli._cfg(cfg, "project")
    zone = cli._cfg(cfg, "zone")
    name = cli._cfg(cfg, "vm_name")
    if not (project and zone and name):
        raise GcloudError("This connection is missing its Google Cloud project, zone or VM "
                          "name. Edit it in Settings and connect again.")
    if cli._cfg(cfg, "vm_source", cli.VM_PLEXORA) == cli.VM_EXISTING:
        raise GcloudError(
            f"“{name}” is a VM you already run. The website cannot install anything on it "
            "(that needs SSH): connect it from the desktop app, or let Plexora provide a VM.")
    if not cli.wants_external_ip(cfg):
        raise GcloudError("The website reaches its VM at the VM's own address, so this "
                          "connection needs “Give the VM a public IP address” on.")
    port = NODE_PORT
    notes = ensure_node_firewall(project, port)
    described = _described(project, zone, name)
    if described is None:
        what = f"Could not create the VM “{name}”."
        try:
            operation = _call("POST", f"{compute_api()}/projects/{_q(project)}/zones/{_q(zone)}"
                                      "/instances", what,
                              body=node_instance_body(cfg, secrets.token_hex(16), port))
            if block:
                _wait(operation, project, what, timeout=cli.CREATE_TIMEOUT)
        except GcloudError as exc:
            if exc.reauth:
                raise
            # The CLI backend's sentences: Google's words are the same
            # (quota, ZONE_RESOURCE_POOL_EXHAUSTED, SERVICE_DISABLED, billing).
            sentence, recovery = cli._create_failure(cfg, exc.detail)
            raise GcloudError(sentence, exc.detail, recovery=recovery) from None
        return {"action": "created", "notes": notes}
    status = str(described.get("status") or "").upper()
    action = "reused"
    if not _metadata(described).get(META_TOKEN):
        if status not in ("TERMINATED", "STOPPED"):
            raise GcloudError(
                f"“{name}” is running for the desktop app's connection. Stop it here, then "
                "connect again: the website turns it into a node while it is stopped.")
        _convert(cfg, described, port)
        action = "converted"
    if status in ("TERMINATED", "STOPPED"):
        start_instance(cfg, block=block)
        action = "started" if action == "reused" else action
    elif status == "SUSPENDED":
        resume_instance(cfg, block=block)
        action = "resumed"
    elif status != "RUNNING":
        action = "waiting"
    return {"action": action, "notes": notes}


def _convert(cfg, described, port):
    """Give a stopped VM the desktop made the web node's metadata and tag."""
    project = cli._cfg(cfg, "project")
    zone = cli._cfg(cfg, "zone")
    name = cli._cfg(cfg, "vm_name")
    url = _instance_url(project, zone, name)
    current = _metadata(described)
    current.update(_node_metadata(cfg, secrets.token_hex(16), port))
    what = f"Could not prepare “{name}” as a web node."
    operation = _call("POST", url + "/setMetadata", what, body={
        "fingerprint": (described.get("metadata") or {}).get("fingerprint", ""),
        "items": [{"key": key, "value": value} for key, value in current.items()]})
    _wait(operation, project, what)
    tags = (described.get("tags") or {})
    items = list(tags.get("items") or ())
    for tag in (cli.NET_TAG, NODE_TAG):
        if tag not in items:
            items.append(tag)
    operation = _call("POST", url + "/setTags", what,
                      body={"fingerprint": tags.get("fingerprint", ""), "items": items})
    _wait(operation, project, what)


def guest_attributes(project, zone, name):
    """`{key: value}` the VM's startup script wrote under `plexora/`, or {}."""
    got = _call("GET", _instance_url(project, zone, name)
                + f"/getGuestAttributes?queryPath={_q(GUEST_NAMESPACE + '/')}",
                f"Could not read what “{name}” is doing.", missing=None) or {}
    items = ((got.get("queryValue") or {}).get("items")) or ()
    return {str(item.get("key")): str(item.get("value") or "") for item in items
            if isinstance(item, dict) and item.get("key")}


def node_status(cfg):
    """Where this profile's web node is: `{status, state, ready, ip, port,
    fingerprint, app_url, pair_url}`.

    `state` is the startup script's own word (installing, starting, ready,
    failed-install, failed-mount, failed-start, failed-noapp: a Plexora
    whose install has no web app build to serve at /app/), or booting before
    it has said anything. `pair_url` is the hand-over: the node's own `/app/`, with
    the node's token in the fragment (never sent to a server) -- read here
    from the VM's metadata, where it lives, and held nowhere."""
    project = cli._cfg(cfg, "project")
    zone = cli._cfg(cfg, "zone")
    name = cli._cfg(cfg, "vm_name")
    out = {"status": "missing", "state": "", "ready": False, "ip": "", "port": NODE_PORT,
           "fingerprint": "", "app_url": "", "pair_url": ""}
    described = _described(project, zone, name)
    if described is None:
        return out
    view = _view(described)
    meta = _metadata(described)
    try:
        port = int(meta.get(META_PORT) or NODE_PORT)
    except ValueError:
        port = NODE_PORT
    out.update(status=view["status"] or "UNKNOWN", ip=view["external_ip"], port=port,
               node=bool(meta.get(META_TOKEN)))
    if view["status"] != "RUNNING":
        return out
    attributes = guest_attributes(project, zone, name)
    out["state"] = attributes.get("state") or "booting"
    out["fingerprint"] = attributes.get("fingerprint") or ""
    node_token = meta.get(META_TOKEN) or ""
    if out["state"] == "ready" and view["external_ip"] and node_token:
        from plexora.connect import pairing_url

        base = f"https://{view['external_ip']}:{port}"
        out["ready"] = True
        out["app_url"] = base + "/app/"
        out["pair_url"] = pairing_url(base + "/app", base, node_token)
    return out
