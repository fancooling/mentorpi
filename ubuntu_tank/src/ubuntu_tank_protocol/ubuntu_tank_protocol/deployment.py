"""Read the host's fail-closed admission gate without ROS or Docker access.

Production Compose mounts a root-owned directory and supplies a unique deployment
token. Missing, replaced or prior-boot approvals disable Start and Arm. Source
product tests without a configured gate retain their standalone behavior.
"""

import json
import os
from pathlib import Path


def admitted() -> bool:
    """Allow control only for the verified pair, generation and current host boot."""
    directory = os.environ.get("UBUNTU_TANK_DEPLOYMENT_DIR")
    if directory is None:
        return True
    try:
        value = json.loads((Path(directory) / "ready.json").read_text())
        return (
            value["token"] == os.environ["UBUNTU_TANK_DEPLOYMENT_TOKEN"]
            and value["release_id"] == os.environ["UBUNTU_TANK_RELEASE_ID"]
            and value["boot_id"]
            == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def validate_identity() -> None:
    """Reject mismatched image builds at production startup, before serving IPC."""
    if "UBUNTU_TANK_DEPLOYMENT_DIR" not in os.environ:
        return
    selected = json.loads(
        (Path(os.environ["UBUNTU_TANK_DEPLOYMENT_DIR"]) / "selected.json").read_text()
    )
    if selected != {
        "token": os.environ["UBUNTU_TANK_DEPLOYMENT_TOKEN"],
        "release_id": os.environ["UBUNTU_TANK_RELEASE_ID"],
    }:
        raise RuntimeError("Container belongs to an obsolete deployment")
    value = json.loads(
        Path("/opt/ubuntu_tank/manifests/build-identity.json").read_text()
    )
    if value["context_sha256"] != os.environ["UBUNTU_TANK_CONTEXT_SHA256"]:
        raise RuntimeError("Image build does not match the selected deployment")
