"""HTTPS for a node that has no certificate: `plexora node serve --tls self-signed`.

The website hands somebody to a node on a Google Cloud VM at
`https://<address>:<port>/app/` (plan L9): the node's own origin, so the page
and the data are one origin -- no CORS, no local-network prompt -- and HTTPS,
because the app needs a secure context (service worker, SharedArrayBuffer).
There is no name to get a public certificate for (an ephemeral address, and
no BioCognia DNS service), so the node makes its own, and the browser warns
once. The VM's startup script publishes the certificate's SHA-256
fingerprint through the Compute API, so the page can show it beside the link
and the warning can be checked rather than clicked through.

**Made once and kept** (`<data root>/node-tls/`, the key readable by its
owner only): an exception the browser remembers is for this certificate, and
a new one on every restart would be a new warning on every restart.

**Waitress stays the server.** It does not speak TLS, so `TlsFront` accepts
TLS on the node's port and passes the decrypted bytes to Waitress listening
on loopback -- one thread per connection, both directions in one `select`
loop (an SSL socket must not be read and written from two threads). Every
request still meets the node's own token check (`api.require_node_token`),
which trusts no address, so a request arriving from loopback this way gains
nothing.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import select
import socket
import ssl
import threading
from pathlib import Path

#: The folder under the data root the certificate and key are kept in.
FOLDER = "node-tls"
CERT = "cert.pem"
KEY = "key.pem"
#: Inside the 398 days browsers allow a certificate, and renewed when past it.
VALID_DAYS = 397
#: Bytes moved per read.
CHUNK = 64 * 1024
#: How long a client may take over its TLS handshake.
HANDSHAKE_SECONDS = 15


class TlsError(RuntimeError):
    """A certificate that could not be made or loaded, in words."""


def certificate(directory, hosts=()):
    """`(cert_path, key_path)` in `directory`: kept from before when still
    valid, otherwise a new self-signed P-256 certificate for `hosts` (names or
    addresses) plus localhost and 127.0.0.1."""
    directory = Path(directory)
    cert_path, key_path = directory / CERT, directory / KEY
    if cert_path.is_file() and key_path.is_file() and _still_valid(cert_path):
        return cert_path, key_path
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - biocognia brings it
        raise TlsError("--tls self-signed needs the `cryptography` package.") from exc
    import ipaddress

    key = ec.generate_private_key(ec.SECP256R1())
    names = []
    for host in ("localhost", "127.0.0.1", *[str(one) for one in hosts if one]):
        try:
            entry = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            entry = x509.DNSName(host)
        if entry not in names:
            names.append(entry)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Plexora node"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Plexora (self-signed)")])
    now = datetime.datetime.now(datetime.timezone.utc)
    built = (x509.CertificateBuilder()
             .subject_name(subject).issuer_name(subject)
             .public_key(key.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(minutes=5))
             .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
             .add_extension(x509.SubjectAlternativeName(names), critical=False)
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .sign(key, hashes.SHA256()))
    directory.mkdir(parents=True, exist_ok=True)
    key_bytes = key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key_bytes)
    cert_path.write_bytes(built.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _still_valid(cert_path):
    try:
        from cryptography import x509

        loaded = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        ends = loaded.not_valid_after_utc
    except Exception:  # noqa: BLE001 -- unreadable is not valid
        return False
    return ends - datetime.datetime.now(datetime.timezone.utc) > datetime.timedelta(days=7)


def fingerprint(cert_path):
    """The SHA-256 fingerprint as browsers and `openssl x509 -fingerprint`
    print it: upper-case hex pairs joined by colons."""
    der = ssl.PEM_cert_to_DER_cert(Path(cert_path).read_text("ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[at:at + 2] for at in range(0, len(digest), 2))


class TlsFront:
    """TLS on `(host, port)`, passed through to a plain server on loopback."""

    def __init__(self, host, port, backend_port, cert_path, key_path, log=print):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["http/1.1"])
        try:
            context.load_cert_chain(str(cert_path), str(key_path))
        except (OSError, ssl.SSLError) as exc:
            raise TlsError(f"Could not load the node's certificate: {exc}") from None
        self.context = context
        self.backend_port = int(backend_port)
        self.log = log
        family = socket.AF_INET6 if ":" in str(host) else socket.AF_INET
        self.listener = socket.create_server((host, int(port)), family=family, backlog=64)
        self.port = self.listener.getsockname()[1]
        self._closed = threading.Event()

    def serve_forever(self):
        while not self._closed.is_set():
            try:
                client, _address = self.listener.accept()
            except OSError:
                if self._closed.is_set():
                    return
                continue
            threading.Thread(target=self._connection, args=(client,), daemon=True,
                             name="plexora-node-tls").start()

    def start(self):
        thread = threading.Thread(target=self.serve_forever, daemon=True,
                                  name="plexora-node-tls-accept")
        thread.start()
        return thread

    def close(self):
        self._closed.set()
        try:
            self.listener.close()
        except OSError:
            pass

    def _connection(self, client):
        secure = backend = None
        try:
            client.settimeout(HANDSHAKE_SECONDS)
            secure = self.context.wrap_socket(client, server_side=True)
            secure.settimeout(None)
            backend = socket.create_connection(("127.0.0.1", self.backend_port))
            self._pump(secure, backend)
        except (OSError, ssl.SSLError, ValueError):
            # A client that refused the certificate (the browser's warning,
            # before the person accepted it), or went away: nothing to say.
            pass
        finally:
            for one in (backend, secure or client):
                if one is not None:
                    try:
                        one.close()
                    except OSError:
                        pass

    @staticmethod
    def _pump(secure, backend):
        """Both directions until either side closes."""
        while True:
            readable, _w, _x = select.select([secure, backend], [], [], 300)
            if not readable:
                return
            if secure in readable:
                data = secure.recv(CHUNK)
                if not data:
                    return
                backend.sendall(data)
                while secure.pending():
                    more = secure.recv(CHUNK)
                    if not more:
                        return
                    backend.sendall(more)
            if backend in readable:
                data = backend.recv(CHUNK)
                if not data:
                    return
                secure.sendall(data)
