"""Private event-loop progress and controller admission for container supervision.

Records use monotonic time and process identity; callers must beat from the work
loop being watched. Files live on runtime-private tmpfs, never on shared IPC.
"""

import json
import os
import signal
import time
import uuid
from pathlib import Path

PROGRESS_DEADLINE = 0.5
POLL_INTERVAL = 0.05
API_TIMEOUT = 0.2
STARTUP_DEADLINE = 10.0
KILL_GRACE = 0.5


def private_dir() -> Path:
    """Return the private tmpfs path configured by the runtime entrypoint."""
    return Path(os.environ.get("UBUNTU_TANK_PRIVATE_DIR", "/run/ubuntu_tank-private"))


def enabled() -> bool:
    """Whether this process participates in container reciprocal supervision."""
    return "UBUNTU_TANK_PRIVATE_DIR" in os.environ


def write_record(name: str, value: dict) -> None:
    """Atomically publish an internal record with owner-only permissions."""
    directory = private_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = directory / f".{name}.{os.getpid()}.{uuid.uuid4().hex}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
    os.replace(temporary, directory / name)


def read_record(name: str) -> dict:
    """Read a bounded record; missing, malformed or oversized records fail closed."""
    try:
        with (private_dir() / name).open() as stream:
            value = json.loads(stream.read(4096))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def beat(name: str, **fields) -> None:
    """Publish progress after completing a work-loop iteration."""
    if enabled():
        write_record(name, dict(fields, pid=os.getpid(), time=time.monotonic()))


def fresh(
    name: str, pid: int | None = None, deadline: float = PROGRESS_DEADLINE
) -> bool:
    """Require recent progress from the expected process, rejecting future time."""
    record = read_record(name)
    try:
        age = time.monotonic() - record["time"]
        return 0 <= age <= deadline and (pid is None or record["pid"] == pid)
    except (KeyError, TypeError):
        return False


def revoke() -> None:
    """Cancel all pending starts and tell the operator to invalidate its epoch."""
    (private_dir() / "permit").unlink(missing_ok=True)
    write_record("epoch", {"value": uuid.uuid4().hex})


def signal_group(pid: int, sig: int) -> None:
    """Signal a known dedicated process group, including stopped descendants."""
    if pid <= 1 or pid == os.getpgrp():
        return
    try:
        os.killpg(pid, sig)
        if sig != signal.SIGKILL:
            os.killpg(pid, signal.SIGCONT)
    except ProcessLookupError:
        pass


def controller_groups() -> set[int]:
    """Return registered runner/ROS group identities for API-independent stopping."""
    record = read_record("controller")
    return {int(record.get(key, 0)) for key in ("pid", "graph_pid")} - {0}


def stop_groups(groups: set[int]) -> None:
    """Request graceful zero/disarm, then kill complete groups within 500 ms."""
    for pid in groups:
        signal_group(pid, signal.SIGINT)
    time.sleep(KILL_GRACE)
    for pid in groups:
        signal_group(pid, signal.SIGKILL)
