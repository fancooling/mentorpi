"""
TLS Certificate Provisioning and Management for MentorPi Tank Web Service.

Generates and provisions owner-trusted X.509 server certificates with Subject
Alternative Names (SANs) for local LAN IP addresses and hostnames using the
cryptography library, satisfying browser and Progressive Web App (PWA) requirements
without requiring cloud-managed certificates or external internet access.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
from collections.abc import Sequence

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def validate_tls_certificate(cert_path: str, key_path: str) -> tuple[str, str]:
    """Validate a mounted, current TLS identity without writing either file.

    Raise on missing, malformed, expired, not-yet-valid or mismatched material.
    Certificate provisioning is a separate owner operation.
    """
    with open(cert_path, "rb") as stream:
        cert = x509.load_pem_x509_certificate(stream.read())
    now = datetime.datetime.now(datetime.timezone.utc)
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ValueError("Mounted TLS certificate is outside its validity period")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return cert_path, key_path


def generate_self_signed_cert(
    cert_path: str,
    key_path: str,
    hostnames_or_ips: Sequence[str] = ("127.0.0.1", "localhost"),
    validity_days: int = 730,
) -> tuple[str, str]:
    """
    Generate an X.509 server certificate and 2048-bit RSA private key.

    Saves the private key with 0600 permissions and certificate with 0644 permissions.
    Adds Subject Alternative Names (SAN) for all supplied IP addresses and hostnames.
    Returns (cert_path, key_path).
    """
    # 1. Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # 2. Build Subject and Issuer Names
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MentorPi"),
            x509.NameAttribute(
                NameOID.COMMON_NAME, "MentorPi Tank Web Control Service"
            ),
        ]
    )

    # 3. Build Subject Alternative Names
    san_entries: list[x509.GeneralName] = []
    seen = set()
    for host in hostnames_or_ips:
        clean_host = host.strip()
        if not clean_host or clean_host in seen:
            continue
        seen.add(clean_host)
        # Strip scheme or port if accidentally provided
        if clean_host.startswith(("http://", "https://")):
            clean_host = clean_host.split("://", 1)[1]
        if ":" in clean_host and not clean_host.startswith("["):
            clean_host = clean_host.split(":", 1)[0]
        elif clean_host.startswith("[") and "]" in clean_host:
            clean_host = clean_host[1 : clean_host.index("]")]

        try:
            ip_obj = ipaddress.ip_address(clean_host)
            san_entries.append(x509.IPAddress(ip_obj))
        except ValueError:
            san_entries.append(x509.DNSName(clean_host))

    # Always ensure 127.0.0.1 and localhost are present
    if not any(
        isinstance(e, x509.IPAddress) and str(e.value) == "127.0.0.1"
        for e in san_entries
    ):
        san_entries.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    if not any(
        isinstance(e, x509.DNSName) and e.value == "localhost" for e in san_entries
    ):
        san_entries.append(x509.DNSName("localhost"))

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .sign(private_key, hashes.SHA256(), default_backend())
    )

    # 4. Write key and cert to disk with secure permissions
    os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(cert_path)), exist_ok=True)

    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)

    # Write key with 0600 permissions
    key_fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(key_fd, "wb") as f:
        f.write(key_bytes)
    os.chmod(key_path, 0o600)

    # Write cert with 0644 permissions
    cert_fd = os.open(cert_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with open(cert_fd, "wb") as f:
        f.write(cert_bytes)
    os.chmod(cert_path, 0o644)

    return cert_path, key_path


def ensure_tls_certificate(
    cert_path: str,
    key_path: str,
    hostnames_or_ips: Sequence[str] = ("127.0.0.1", "localhost"),
    validity_days: int = 730,
    valid_days: int | None = None,
) -> tuple[str, str]:
    """Verify that cert and key exist and are valid; generate them if missing or corrupted."""
    v_days = valid_days if valid_days is not None else validity_days
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        try:
            with open(cert_path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            with open(key_path, "rb") as f:
                serialization.load_pem_private_key(f.read(), password=None)
            now = datetime.datetime.now(datetime.timezone.utc)
            if cert.not_valid_after_utc > now:
                return cert_path, key_path
        except Exception:
            pass

    return generate_self_signed_cert(
        cert_path=cert_path,
        key_path=key_path,
        hostnames_or_ips=hostnames_or_ips,
        validity_days=v_days,
    )
