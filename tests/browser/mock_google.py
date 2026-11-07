"""A stand-in Google for the website's Google Cloud tests: never the real one.

    python tests/browser/mock_google.py [--port 0] [--client-id ID] [--ip 203.0.113.10]
        [--ready-after 2]

What Plexora asks of Google, answered in Google's own shapes, from an
in-memory project that nothing outside this process can see:

- the OAuth 2.0 authorization endpoint (`/o/oauth2/v2/auth`): the page's
  sign-in popup (services/googleAuth.js) lands here and is sent straight back
  to its `redirect_uri` with `#access_token=...&expires_in=3600&scope=...&state=`,
  as Google does once a person has consented -- after checking the client id,
  `response_type=token` and the scopes;
- OpenID Connect's userinfo (`/v1/userinfo`), Resource Manager's project list
  (`/v1/projects`), Cloud Storage's JSON API (`/storage/v1/b...`) and Compute
  Engine's (`/compute/v1/projects/...`: zones, instances, aggregated
  instances, insert/start/stop/delete/setMachineType/setMetadata/setTags,
  getGuestAttributes, firewalls, operations' wait). Every API route needs
  `Authorization: Bearer <a token this stand-in issued>`, or answers 401 as
  Google does; CORS as Google's APIs send it (the Origin echoed,
  Authorization and Content-Type allowed).

A VM inserted here is RUNNING at once with the address `--ip`; its guest
attributes answer 404 for the first poll (the startup script has said
nothing), then `state=installing`, then -- from poll `--ready-after` on --
`state=ready` and a certificate fingerprint, as `gcloud_rest.NODE_STARTUP_SCRIPT`
writes them on a real VM.

For the driver: `GET /_requests` (method, path, whether a bearer token came --
never its value), `GET /_state` (instances with their metadata, firewalls,
the auth requests' scopes), `POST /_expire` (every token stops working: the
hour has passed). Prints one JSON line `{"port"}` when listening.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

FINGERPRINT = ":".join(["AB"] * 32)


class World:
    """The project Google would hold: buckets, zones, VMs, firewall rules."""

    def __init__(self, client_id="test-client.apps.googleusercontent.com", ip="203.0.113.10",
                 ready_after=2):
        self.client_id = client_id
        self.ip = ip
        self.ready_after = ready_after
        self.tokens = set()
        self.lock = threading.Lock()
        self.requests = []
        self.auth_requests = []
        self.projects = [{"projectId": "lab-imaging", "name": "Lab imaging",
                          "lifecycleState": "ACTIVE"},
                         {"projectId": "teaching-2026", "name": "Teaching",
                          "lifecycleState": "ACTIVE"}]
        self.buckets = {
            "tonsil-data": {"name": "tonsil-data", "location": "US-EAST1",
                            "locationType": "region"},
            "atlas-public": {"name": "atlas-public", "location": "US",
                             "locationType": "multi-region"},
        }
        self.objects = {
            "tonsil-data": ["README.txt", "images/tonsil.ome.tif", "images/masks/cells.tif",
                            "images/cells.csv"],
            "atlas-public": ["atlas/region-1.ome.tif"],
        }
        self.zones = [("us-east1-b", "us-east1", "UP"), ("us-east1-c", "us-east1", "UP"),
                      ("us-east1-d", "us-east1", "DOWN"), ("us-central1-a", "us-central1", "UP")]
        self.instances = {}
        self.firewalls = {"default-allow-ssh": {"name": "default-allow-ssh",
                                                "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
                                                "sourceRanges": ["0.0.0.0/0"]}}
        self.ops = 0

    def issue(self):
        token = "ya29.test-" + secrets.token_hex(12)
        self.tokens.add(token)
        return token

    def op(self, zone=""):
        self.ops += 1
        out = {"kind": "compute#operation", "name": f"operation-{self.ops}", "status": "DONE"}
        if zone:
            out["zone"] = f"https://compute.googleapis.com/compute/v1/projects/p/zones/{zone}"
        return out


def make_handler(world: World):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Cross-Origin-Resource-Policy", "cross-origin")

        def _send(self, status, payload, extra=()):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=UTF-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, message, reason="", state=""):
            self._send(status, {"error": {"code": status, "message": message,
                                          "status": state,
                                          "errors": [{"reason": reason or state.lower(),
                                                      "message": message}]}})

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             self.headers.get("Access-Control-Request-Headers")
                             or "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw or b"null")
            except ValueError:
                return None

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_DELETE(self):  # noqa: N802
            self._handle("DELETE")

        def _handle(self, method):
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            body = self._body() if method in ("POST", "DELETE") else None
            if path.startswith("/_"):
                return self._driver(method, path)
            if path == "/o/oauth2/v2/auth":
                return self._authorize(query)
            auth = self.headers.get("Authorization") or ""
            with world.lock:
                world.requests.append({"method": method, "path": path,
                                       "bearer": auth.startswith("Bearer ")})
            if not (auth.startswith("Bearer ") and auth[7:] in world.tokens):
                return self._error(401, "Request had invalid authentication credentials. "
                                        "Expected OAuth 2 access token.",
                                   "authError", "UNAUTHENTICATED")
            with world.lock:
                return self._api(method, path, query, body or {})

        # -- the driver's own routes ----------------------------------------------

        def _driver(self, method, path):
            with world.lock:
                if path == "/_requests":
                    return self._send(200, world.requests)
                if path == "/_state":
                    return self._send(200, {"instances": world.instances,
                                            "firewalls": world.firewalls,
                                            "auth": world.auth_requests})
                if path == "/_expire" and method == "POST":
                    world.tokens.clear()
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "no such driver route"})

        # -- OAuth ----------------------------------------------------------------

        def _authorize(self, query):
            one = {key: values[0] for key, values in query.items()}
            with world.lock:
                world.auth_requests.append({key: one.get(key) for key in
                                            ("client_id", "redirect_uri", "response_type",
                                             "scope", "prompt", "include_granted_scopes")})
            redirect = one.get("redirect_uri") or ""
            if one.get("client_id") != world.client_id or not redirect:
                body = b"Error 401: invalid_client"
                self.send_response(401)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            if one.get("response_type") != "token":
                fragment = urlencode({"error": "unsupported_response_type",
                                      "state": one.get("state", "")})
            else:
                with world.lock:
                    token = world.issue()
                fragment = urlencode({"access_token": token, "token_type": "Bearer",
                                      "expires_in": "3599", "scope": one.get("scope", ""),
                                      "state": one.get("state", "")})
            self.send_response(302)
            self.send_header("Location", f"{redirect}#{fragment}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        # -- the APIs --------------------------------------------------------------

        def _api(self, method, path, query, body):
            if path == "/v1/userinfo":
                return self._send(200, {"sub": "1234567890", "email": "researcher@example.org",
                                        "email_verified": True})
            if path == "/v1/projects":
                return self._send(200, {"projects": world.projects})
            match = re.fullmatch(r"/storage/v1/b(?:/([^/]+)(/o)?)?", path)
            if match:
                return self._storage(match.group(1), bool(match.group(2)), query)
            match = re.fullmatch(r"/compute/v1/projects/([^/]+)(/.*)", path)
            if match:
                return self._compute(method, match.group(1), match.group(2), query, body)
            return self._error(404, f"The requested URL {path} was not found.", "notFound",
                               "NOT_FOUND")

        def _storage(self, name, listing, query):
            if name is None:
                return self._send(200, {"kind": "storage#buckets",
                                        "items": list(world.buckets.values())})
            if name not in world.buckets:
                return self._error(404, "The specified bucket does not exist.", "notFound")
            if not listing:
                return self._send(200, world.buckets[name])
            prefix = (query.get("prefix") or [""])[0]
            folders, items = set(), []
            for key in world.objects.get(name, ()):
                if not key.startswith(prefix):
                    continue
                rest = key[len(prefix):]
                if "/" in rest:
                    folders.add(prefix + rest.split("/", 1)[0] + "/")
                else:
                    items.append({"name": key, "size": str(1024 * (len(key) + 1)),
                                  "updated": "2026-11-01T10:00:00.000Z"})
            return self._send(200, {"kind": "storage#objects", "prefixes": sorted(folders),
                                    "items": items})

        def _compute(self, method, project, rest, query, body):
            if rest == "/zones":
                return self._send(200, {"items": [
                    {"name": zone, "status": status,
                     "region": f"https://compute.googleapis.com/compute/v1/projects/{project}"
                               f"/regions/{region}"} for zone, region, status in world.zones]})
            if rest == "/aggregated/instances":
                grouped = {}
                for described in world.instances.values():
                    zone = described["zone"].rsplit("/", 1)[-1]
                    grouped.setdefault(f"zones/{zone}", {"instances": []})["instances"].append(
                        described)
                return self._send(200, {"items": grouped})
            if rest == "/global/firewalls":
                if method == "POST":
                    world.firewalls[body["name"]] = body
                    return self._send(200, world.op())
                return self._send(200, {"items": list(world.firewalls.values())})
            match = re.fullmatch(r"/(?:zones/([^/]+)|global)/operations/([^/]+)/wait", rest)
            if match:
                return self._send(200, {"name": match.group(2), "status": "DONE"})
            match = re.fullmatch(r"/zones/([^/]+)/instances(?:/([^/]+)(?:/(\w+))?)?", rest)
            if not match:
                return self._error(404, f"{rest} was not found.", "notFound", "NOT_FOUND")
            zone, name, verb = match.groups()
            if name is None:
                if method == "POST":
                    return self._insert(project, zone, body)
                return self._send(200, {"items": [one for one in world.instances.values()
                                                  if one["zone"].endswith("/" + zone)]})
            described = world.instances.get(name)
            if described is None or not described["zone"].endswith("/" + zone):
                return self._error(404, f"The resource 'projects/{project}/zones/{zone}/"
                                        f"instances/{name}' was not found", "notFound",
                                   "NOT_FOUND")
            if verb is None and method == "GET":
                return self._send(200, described)
            if verb is None and method == "DELETE":
                del world.instances[name]
                return self._send(200, world.op(zone))
            if verb == "getGuestAttributes":
                described["_polls"] = described.get("_polls", 0) + 1
                polls = described["_polls"]
                if polls < 2:
                    return self._error(404, "The resource 'guest attributes' was not found",
                                       "notFound", "NOT_FOUND")
                items = [{"namespace": "plexora", "key": "state",
                          "value": "ready" if polls >= world.ready_after + 1 else "installing"}]
                if polls >= world.ready_after + 1:
                    items.append({"namespace": "plexora", "key": "fingerprint",
                                  "value": FINGERPRINT})
                return self._send(200, {"queryPath": "plexora/", "queryValue": {"items": items}})
            if verb == "start":
                described["status"] = "RUNNING"
                described["_polls"] = 0
            elif verb == "stop":
                described["status"] = "TERMINATED"
            elif verb == "setMachineType":
                if described["status"] != "TERMINATED":
                    return self._error(400, "The resource is not in a valid state.",
                                       "resourceNotReady")
                described["machineType"] = body.get("machineType", "")
            elif verb == "setMetadata":
                described["metadata"] = {"items": body.get("items", []), "fingerprint": "m2"}
            elif verb == "setTags":
                described["tags"] = {"items": body.get("items", []), "fingerprint": "t2"}
            else:
                return self._error(400, f"Unknown verb {verb}", "invalid")
            return self._send(200, world.op(zone))

        def _insert(self, project, zone, body):
            name = body.get("name") or ""
            if name in world.instances:
                return self._error(409, f"The resource '{name}' already exists",
                                   "alreadyExists", "ALREADY_EXISTS")
            described = dict(body)
            described.update({
                "kind": "compute#instance", "status": "RUNNING",
                "zone": f"https://compute.googleapis.com/compute/v1/projects/{project}"
                        f"/zones/{zone}",
                "machineType": f"https://compute.googleapis.com/compute/v1/projects/{project}"
                               f"/zones/{zone}/machineTypes/"
                               + str(body.get("machineType", "")).rsplit("/", 1)[-1],
                "networkInterfaces": [{"accessConfigs": [{"name": "external-nat",
                                                          "natIP": world.ip}]}],
            })
            described.setdefault("metadata", {})["fingerprint"] = "m1"
            described.setdefault("tags", {})["fingerprint"] = "t1"
            world.instances[name] = described
            return self._send(200, world.op(zone))

    return Handler


def serve(port=0, **options):
    """`(server, world)`, listening, in a daemon thread."""
    world = World(**options)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(world))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, world


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--client-id", default="test-client.apps.googleusercontent.com")
    parser.add_argument("--ip", default="203.0.113.10")
    parser.add_argument("--ready-after", type=int, default=2)
    args = parser.parse_args(argv)
    server, _world = serve(args.port, client_id=args.client_id, ip=args.ip,
                           ready_after=args.ready_after)
    print(json.dumps({"port": server.server_address[1]}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
