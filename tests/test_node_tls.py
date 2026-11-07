"""`plexora node serve --tls self-signed`: HTTPS for a node a browser opens itself.

The website hands somebody to a node on a cloud VM at its own address (plan
L9), which needs HTTPS and has no name to get a certificate for. Pinned here:
the certificate is made once and kept (an exception the browser remembers
must survive a restart), its fingerprint is printed in the form browsers and
`openssl x509 -fingerprint -sha256` show, the TLS front passes requests and
answers through to Waitress unchanged (keep-alive included), and the node's
token is still required behind it.
"""

import hashlib
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plexora.server.node import tls


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _unverified():
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def test_the_certificate_is_made_once_and_kept(tmp_path):
    cert, key = tls.certificate(tmp_path / "node-tls", hosts=["203.0.113.10", "vm.example"])
    first = cert.read_bytes()
    again, _ = tls.certificate(tmp_path / "node-tls")
    assert again.read_bytes() == first
    if os.name != "nt":
        assert (key.stat().st_mode & 0o777) == 0o600
    from cryptography import x509

    loaded = x509.load_pem_x509_certificate(first)
    names = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "vm.example" in names.get_values_for_type(x509.DNSName)
    printed = tls.fingerprint(cert)
    der = ssl.PEM_cert_to_DER_cert(first.decode("ascii"))
    assert printed.replace(":", "") == hashlib.sha256(der).hexdigest().upper()
    assert len(printed.split(":")) == 32


def test_the_front_passes_requests_through_on_one_connection(tmp_path):
    class Echo(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            body = json.dumps({"path": self.path, "pad": "x" * (300000 if "big" in self.path
                                                                  else 0)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    cert, key = tls.certificate(tmp_path)
    front = tls.TlsFront("127.0.0.1", 0, backend.server_address[1], cert, key)
    front.start()
    try:
        connection = http.client.HTTPSConnection("127.0.0.1", front.port,
                                                 context=_unverified(), timeout=10)
        # The last answer is ~300 KB: many TLS records through one pump.
        for path in ("/app/", "/node/v1/hello?x=1", "/big/" + "a" * 4000):
            connection.request("GET", path)
            answer = connection.getresponse()
            assert answer.status == 200
            assert json.loads(answer.read())["path"] == path
        peer = connection.sock.getpeercert(binary_form=True)
        assert hashlib.sha256(peer).hexdigest().upper() == tls.fingerprint(cert).replace(":", "")
        connection.close()
        # Plain HTTP on the TLS port is refused, not answered.
        with socket.create_connection(("127.0.0.1", front.port), timeout=5) as raw:
            raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                said = raw.recv(100)
            except OSError:
                said = b""
            assert not said.startswith(b"HTTP/1.1 200")
    finally:
        front.close()
        backend.shutdown()


@pytest.mark.timeout(180)
def test_node_serve_with_tls_answers_https_and_still_wants_its_token():
    port = _free_port()
    token = "a1b2c3d4e5f60718"
    root = tempfile.mkdtemp(prefix="plexora-node-tls-")
    env = dict(os.environ, PLEXORA_DATA_PATH=root, PLEXORA_NODE_TOKEN=token,
               PYTHONUNBUFFERED="1")
    process = subprocess.Popen(
        [sys.executable, "-m", "plexora", "node", "serve", "--host", "127.0.0.1",
         "--port", str(port), "--tls", "self-signed", "--dynamic"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 150
        answer = None
        while time.monotonic() < deadline:
            assert process.poll() is None, process.stdout.read()
            try:
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=_unverified(),
                                                         timeout=5)
                connection.request("GET", "/node/v1/health",
                                   headers={"X-Plexora-Node-Token": token})
                answer = connection.getresponse()
                break
            except (OSError, ssl.SSLError):
                time.sleep(0.3)
        assert answer is not None and answer.status == 200, answer and answer.read()
        answer.read()
        connection.request("GET", "/node/v1/hello")
        refused = connection.getresponse()
        assert refused.status == 403
        refused.read()
        connection.close()
        cert = os.path.join(root, "node-tls", "cert.pem")
        assert os.path.isfile(cert)
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=20)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate()[0]
    assert f"Serving on https://127.0.0.1:{port}" in output
    assert "certificate SHA-256 " + tls.fingerprint(cert) in output
    # The token is the node's own announce line's business, never the URL's.
    assert f"https://127.0.0.1:{port}/?t=" not in output
