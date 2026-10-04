"""Publish only the enrolled Aurora USB node into runtime's private USB tree.

Root-only host helper invoked by prepare-host, boot/deploy, and udev USB events.
The first observed single 3251:1930 camera is enrolled by serial. Later refreshes
retain that identity, remove stale nodes and publish only its current address.
No USB device is opened. /dev/bus/usb on the host is never mounted wholesale.
"""

import fcntl
import json
import os
import re
import stat
from pathlib import Path

ROOT = Path("/dev/ubuntu-tank-camera-usb")
IDENTITY = Path("/var/lib/ubuntu_tank-container/camera.json")


def refresh() -> str:
    """Reconcile sysfs enumeration to private device nodes; return enrolled serial.

    Requires root/mknod on the real Pi. An absent enrolled camera leaves an empty
    tree so runtime can report unavailable and recover after a udev add event.
    Multiple unenrolled cameras fail rather than selecting an arbitrary camera.
    """
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = os.open("/run/ubuntu-tank-camera.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        observed = []
        for path in Path("/sys/bus/usb/devices").iterdir():
            try:
                if (path / "idVendor").read_text().strip() != "3251" or (
                    path / "idProduct"
                ).read_text().strip() != "1930":
                    continue
                serial = (path / "serial").read_text().strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]+", serial):
                    raise RuntimeError("Aurora serial has unsupported characters")
                bus = int((path / "busnum").read_text())
                address = int((path / "devnum").read_text())
                source = Path(f"/dev/bus/usb/{bus:03}/{address:03}")
                info = source.stat()
                if stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == 189:
                    observed.append((serial, source, info))
            except (FileNotFoundError, NotADirectoryError):
                continue  # Device disappeared while enumerating.
        selected = (
            json.loads(IDENTITY.read_text())["serial"] if IDENTITY.exists() else ""
        )
        if not selected and observed:
            if len(observed) != 1:
                raise RuntimeError("Connect only one Aurora for initial enrollment")
            selected = observed[0][0]
            IDENTITY.parent.mkdir(parents=True, exist_ok=True)
            temporary = IDENTITY.with_suffix(".tmp")
            temporary.write_text(json.dumps({"serial": selected}) + "\n")
            os.chmod(temporary, 0o600)
            temporary.replace(IDENTITY)
        desired = {}
        for serial, source, info in observed:
            if serial == selected:
                desired[ROOT / source.parent.name / source.name] = info.st_rdev
        for path in ROOT.glob("*/*"):
            if path not in desired or path.stat().st_rdev != desired[path]:
                path.unlink()
        for path, number in desired.items():
            path.parent.mkdir(exist_ok=True)
            if not path.exists():
                os.mknod(path, stat.S_IFCHR | 0o660, number)
            os.chown(path, 0, 10001)
            os.chmod(path, 0o660)
        return selected
    finally:
        os.close(lock)


if __name__ == "__main__":
    refresh()
