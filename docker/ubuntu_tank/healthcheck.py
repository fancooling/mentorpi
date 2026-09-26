#!/usr/bin/env python3
"""Probe readiness without acquiring ownership, starting the controller or arming.

Runtime checks both public IPC servers; a stopped controller is healthy. Web
checks its local HTTPS version endpoint without relying on runtime availability.
The mounted certificate is used as its trust anchor.
"""

import json
import socket
import ssl
import sys
import urllib.request


def request(path: str, payload: dict) -> dict:
    """Send one bounded newline-framed IPC health request."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(1)
        sock.connect(path)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk or len(data) + len(chunk) > 65536:
                raise RuntimeError("Invalid IPC health response")
            data += chunk
        return json.loads(data.split(b"\n", 1)[0])


def main() -> None:
    """Exit nonzero when the selected image service cannot answer a safe probe."""
    if sys.argv[1] == "runtime":
        from ubuntu_tank_protocol.ipc_client import OperatorIpcClient

        with OperatorIpcClient() as client:
            client.get_status()
        if not request("/run/ubuntu_tank/lifecycle.sock", {"action": "status"}).get(
            "success"
        ):
            raise RuntimeError("Lifecycle unavailable")
    else:
        from ubuntu_tank_web.entrypoint import load_config_from_yaml

        cfg = load_config_from_yaml("/etc/opt/ubuntu_tank/web/web.yaml")
        context = ssl.create_default_context(cafile=cfg.tls_cert_path)
        context.check_hostname = False
        with urllib.request.urlopen(
            "https://127.0.0.1:8443/api/v1/version", context=context, timeout=2
        ) as response:
            if response.status != 200:
                raise RuntimeError("Web unavailable")


if __name__ == "__main__":
    main()
