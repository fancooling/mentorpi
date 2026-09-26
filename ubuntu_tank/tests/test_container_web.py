"""Exercise mounted TLS identity and fail-closed production web startup."""

import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
for package in ("ubuntu_tank_protocol", "ubuntu_tank_web"):
    sys.path.insert(0, str(WORKSPACE / "src" / package))

sys.path.insert(0, str(WORKSPACE.parent / "docker/ubuntu_tank"))

from tls_setup import prepare as prepare_tls
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_web.entrypoint import load_config_from_yaml
from ubuntu_tank_web.tls import generate_self_signed_cert, validate_tls_certificate


class MountedTlsTest(unittest.TestCase):
    """Use real certificates and HTTPS; never substitute source/config assertions."""

    def test_first_use_tls_preserves_identity_and_adds_exact_origins(self):
        """Generated TLS is valid, reusable and covers IPv4, IPv6 and DNS names."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "web.yaml"
            config.write_text(
                json.dumps(WebControlConfig().to_dict() | {"linear_speed_cap": 0.12})
            )
            names, ips = ["rpitank", "rpitank.local"], ["192.0.2.4", "2001:db8::4"]
            result = prepare_tls(root, names, ips)
            self.assertEqual(len(result["created"]), 2)
            cert, key = root / "certs/server.crt", root / "certs/server.key"
            validate_tls_certificate(str(cert), str(key))
            before = [path.read_bytes() for path in (cert, key, config)]
            self.assertEqual(prepare_tls(root, names, ips)["created"], [])
            self.assertEqual(
                [path.read_bytes() for path in (cert, key, config)], before
            )
            parsed = load_config_from_yaml(str(config))
            self.assertEqual(parsed.linear_speed_cap, 0.12)
            self.assertIn("https://[2001:db8::4]:8443", parsed.allowed_origins)
            self.assertIn("https://rpitank:8443", parsed.allowed_origins)

    def test_first_use_tls_rejects_existing_identity_problems(self):
        """Invalid pairs and uncovered names are rejected without silent rotation."""
        for failure in ("partial", "corrupt", "expired", "mismatch", "name"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "web.yaml"
                config.write_text(json.dumps(WebControlConfig().to_dict()))
                cert, key = root / "certs/server.crt", root / "certs/server.key"
                generate_self_signed_cert(
                    str(cert),
                    str(key),
                    ["rpitank"],
                    validity_days=0 if failure == "expired" else 1,
                )
                if failure == "partial":
                    key.unlink()
                elif failure == "corrupt":
                    cert.write_text("invalid PEM")
                elif failure == "mismatch":
                    generate_self_signed_cert(str(root / "other.crt"), str(key))
                paths = [path for path in (cert, key, config) if path.exists()]
                before = [path.read_bytes() for path in paths]
                with self.assertRaises((ValueError, ssl.SSLError)):
                    prepare_tls(
                        root, ["other.invalid" if failure == "name" else "rpitank"], []
                    )
                self.assertEqual([path.read_bytes() for path in paths], before)

    def test_rejects_invalid_identity_without_replacing_files(self):
        """Missing, corrupt, expired and mismatched identities fail without mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = str(Path(tmp) / "server.crt"), str(Path(tmp) / "server.key")
            with self.assertRaises(FileNotFoundError):
                validate_tls_certificate(cert, key)
            self.assertFalse(Path(cert).exists())
            for failure in ("corrupt", "expired", "mismatch"):
                with self.subTest(failure=failure):
                    generate_self_signed_cert(
                        cert, key, validity_days=0 if failure == "expired" else 1
                    )
                    if failure == "corrupt":
                        Path(cert).write_text("invalid PEM")
                    if failure == "mismatch":
                        generate_self_signed_cert(str(Path(tmp) / "other.crt"), key)
                    before = Path(cert).read_bytes(), Path(key).read_bytes()
                    with self.assertRaises((ValueError, ssl.SSLError)):
                        validate_tls_certificate(cert, key)
                    self.assertEqual(
                        (Path(cert).read_bytes(), Path(key).read_bytes()), before
                    )

    def test_configuration_errors_do_not_fall_back(self):
        """Missing files and invalid safety settings must prevent server startup."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "web.yaml"
            with self.assertRaises(FileNotFoundError):
                load_config_from_yaml(str(config))
            for text in ("[]", 'allowed_origins: ["*"]', "linear_speed_cap: 999"):
                config.write_text(text)
                with self.assertRaises((ValueError, TypeError)):
                    load_config_from_yaml(str(config))

    def test_https_entrypoint_preserves_mounted_identity(self):
        """A real server serves API/static assets with the supplied certificate."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cert, key = generate_self_signed_cert(
                str(root / "server.crt"), str(root / "server.key")
            )
            before = Path(cert).read_bytes(), Path(key).read_bytes()
            config = root / "web.yaml"
            config.write_text(
                json.dumps(
                    {
                        "tls_cert_path": cert,
                        "tls_key_path": key,
                        "operator_socket_path": str(root / "absent.sock"),
                        "lifecycle_socket_path": str(root / "absent-lifecycle.sock"),
                    }
                )
            )
            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("<html>container web</html>")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    str(WORKSPACE / "src" / p)
                    for p in ("ubuntu_tank_protocol", "ubuntu_tank_web")
                ),
            }
            with (root / "server.log").open("w+") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "ubuntu_tank_web.entrypoint",
                        "--config",
                        str(config),
                        "--port",
                        str(port),
                        "--static-dir",
                        str(static),
                    ],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                try:
                    context = ssl.create_default_context(cafile=cert)
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            with urllib.request.urlopen(
                                f"https://127.0.0.1:{port}/api/v1/version",
                                context=context,
                                timeout=1,
                            ) as response:
                                self.assertEqual(response.status, 200)
                            break
                        except OSError:
                            if (
                                process.poll() is not None
                                or time.monotonic() >= deadline
                            ):
                                log.seek(0)
                                self.fail(log.read())
                            time.sleep(0.05)
                    with urllib.request.urlopen(
                        f"https://127.0.0.1:{port}/", context=context, timeout=1
                    ) as response:
                        self.assertIn(b"container web", response.read())
                finally:
                    process.terminate()
                    process.wait(timeout=5)
            self.assertEqual((Path(cert).read_bytes(), Path(key).read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
