"""A throwaway self-signed certificate for 127.0.0.1, for P5 scenario (b).

    python selfsigned.py DIR   # writes DIR/cert.pem and DIR/key.pem (if absent)

Chrome is started with --ignore-certificate-errors for these runs, so nothing
is installed into any trust store.
"""

import datetime
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def main(directory):
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = out / "cert.pem", out / "key.pem"
    if cert_path.exists() and key_path.exists():
        return
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))


if __name__ == "__main__":
    main(sys.argv[1])
