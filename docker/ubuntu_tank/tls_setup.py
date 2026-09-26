"""Prepare a TLS identity and browser origins using the installed web image.

The host CLI runs this helper against a disposable writable directory containing
web.yaml and certs/. It never receives host Docker access or robot devices. Only
validated files are subsequently installed by the host CLI. Dependencies come
from the existing web image, not the Pi's system Python.
"""

import ipaddress
import json
import re
import sys
from pathlib import Path

import yaml
from cryptography import x509
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_web.tls import generate_self_signed_cert, validate_tls_certificate

CERT_ROOT = Path("/var/opt/ubuntu_tank/web/certs")


def prepare(directory, hostnames, addresses):
    """Validate or create a staged identity and append exact HTTPS origins.

    Return created relative paths and browser origins. Existing valid identities
    are preserved byte-for-byte and must cover all requested names/IPs. Reject
    partial, invalid, expired, mismatched or insufficient certificates without
    replacing them. Configuration paths must stay inside the mounted certs root.
    No host services are contacted. The caller must supply an isolated directory.
    """
    names = []
    for name in hostnames:
        name = name.rstrip(".").encode("idna").decode("ascii").lower()
        if len(name) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in name.split(".")
        ):
            raise ValueError("Use a DNS hostname without scheme, port or wildcard")
        names.append(name)
    ips = [ipaddress.ip_address(value) for value in addresses]
    if not names and not ips:
        raise ValueError("Specify at least one --hostname or --ip")
    directory = Path(directory)
    config_path = directory / "web.yaml"
    data = yaml.safe_load(config_path.read_text())
    cfg = WebControlConfig.from_dict(data)
    paths = []
    for value in (cfg.tls_cert_path, cfg.tls_key_path):
        if not value:
            raise ValueError("Configure TLS certificate and key paths first")
        relative = Path(value).relative_to(CERT_ROOT)
        if not relative.parts or ".." in relative.parts:
            raise ValueError(
                "TLS paths must be files beneath the certificate directory"
            )
        path = directory / "certs" / relative
        if path.is_symlink():
            raise ValueError("Certificate symlinks are unsupported")
        paths.append(path)
    cert, key = paths
    if cert == key:
        raise ValueError("Certificate and key paths must differ")
    present = [path.exists() for path in paths]
    if any(present) and not all(present):
        raise ValueError(
            "Only one TLS file exists; restore the matching pair before retrying"
        )
    created = []
    if not any(present):
        # The existing generator accepts bracketed IPv6 addresses.
        subjects = names + [f"[{ip}]" if ip.version == 6 else str(ip) for ip in ips]
        generate_self_signed_cert(str(cert), str(key), subjects)
        created = [str(path.relative_to(directory / "certs")) for path in paths]
    validate_tls_certificate(str(cert), str(key))
    certificate = x509.load_pem_x509_certificate(cert.read_bytes())
    san = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    dns_names = {name.lower() for name in san.get_values_for_type(x509.DNSName)}
    ip_names = set(san.get_values_for_type(x509.IPAddress))
    if not set(names) <= dns_names or not set(ips) <= ip_names:
        raise ValueError(
            "Existing certificate does not cover requested names/IPs; no replacement was made"
        )
    origins = list(
        dict.fromkeys(
            [f"https://{name}:8443" for name in names]
            + [
                f"https://[{ip}]:8443" if ip.version == 6 else f"https://{ip}:8443"
                for ip in ips
            ]
        )
    )
    allowed = list(cfg.allowed_origins)
    for origin in origins:
        if origin not in allowed:
            allowed.append(origin)
    if allowed != cfg.allowed_origins:
        data["allowed_origins"] = allowed
        WebControlConfig.from_dict(data)
        config_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return {"created": created, "origins": origins}


if __name__ == "__main__":
    options = json.loads(sys.argv[1])
    print(json.dumps(prepare("/setup", options["hostnames"], options["addresses"])))
