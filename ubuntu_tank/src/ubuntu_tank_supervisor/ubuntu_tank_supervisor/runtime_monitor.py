"""Independent runtime safety loop and Supervisor process-exit event listener.

Checks event-loop progress every 50 ms (500 ms deadline), with one bounded
200 ms control-API call per iteration. Infrastructure faults revoke Start,
signal controller groups without RPC, and terminate supervisord for container
recovery. A controller fault stays stopped until an explicit new Start.
"""

import os
import select
import signal
import sys
import time
import xmlrpc.client

from . import progress
from .supervisor_api import call


def main() -> int:
    """Run as Supervisor's event listener; stdout is reserved for its protocol."""
    supervisor_pid = os.getppid()
    born = time.monotonic()
    known = {}
    controller_pid = 0
    controller_seen = 0.0
    buffer = b""
    payload_size = None
    running = True

    def shutdown(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    progress.revoke()
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    reason = "Runtime shutdown"
    try:
        while running:
            iteration = time.monotonic()
            # Consume bounded event data without blocking progress checks.
            if select.select([sys.stdin], [], [], 0)[0]:
                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    raise RuntimeError("Supervisor event channel closed")
                buffer += chunk
                if payload_size is None and b"\n" in buffer:
                    header, buffer = buffer.split(b"\n", 1)
                    fields = dict(part.split(b":", 1) for part in header.split())
                    payload_size = int(fields[b"len"])
                    if payload_size > 4096:
                        raise RuntimeError("Oversized Supervisor event")
                if payload_size is not None and len(buffer) >= payload_size:
                    payload = buffer[:payload_size]
                    buffer = buffer[payload_size:]
                    payload_size = None
                    # RPC state below handles both event and polling detection;
                    # retain the event as diagnostic evidence, never on stdout.
                    print(
                        "Process event: " + payload.decode(errors="replace"),
                        file=sys.stderr,
                    )
                    sys.stdout.write("RESULT 2\nOKREADY\n")
                    sys.stdout.flush()
            infos = {info["name"]: info for info in call("getAllProcessInfo")}
            known = infos
            for name, records in (
                ("operator", ("operator", "operator_ipc")),
                ("lifecycle", ("lifecycle",)),
            ):
                info = infos[name]
                if info["statename"] in (
                    "EXITED",
                    "FATAL",
                    "STOPPED",
                    "BACKOFF",
                    "STOPPING",
                ):
                    raise RuntimeError(f"Required process {name} exited")
                initialized = all(
                    progress.read_record(r).get("pid") == info["pid"] for r in records
                )
                if (
                    iteration - born > progress.STARTUP_DEADLINE or initialized
                ) and not all(progress.fresh(r, info["pid"]) for r in records):
                    raise RuntimeError(f"{name} event loop stopped progressing")
            info = infos["controller"]
            if info["pid"] and info["statename"] != "STOPPING":
                if info["pid"] != controller_pid:
                    controller_pid = info["pid"]
                    controller_seen = iteration
                record = progress.read_record("controller")
                if (
                    record.get("pid") == controller_pid
                    and record.get("phase") != "starting"
                ) or iteration - controller_seen > progress.STARTUP_DEADLINE:
                    deadline = (
                        progress.KILL_GRACE + progress.API_TIMEOUT
                        if record.get("phase") == "stopping"
                        else progress.PROGRESS_DEADLINE
                    )
                    if not progress.fresh("controller", controller_pid, deadline):
                        raise RuntimeError("Controller runner stopped progressing")
            elif not info["pid"] and controller_pid:
                progress.revoke()
                # Runner may have died before reaping its separate ROS group.
                progress.stop_groups(progress.controller_groups())
                progress.write_record("controller", {})
                controller_pid = 0
            progress.beat("monitor")
            time.sleep(max(0, progress.POLL_INTERVAL - (time.monotonic() - iteration)))
    except (OSError, RuntimeError, KeyError, ValueError, xmlrpc.client.Error) as exc:
        reason = str(exc)
    finally:
        print("Runtime safety stop: " + reason, file=sys.stderr, flush=True)
        progress.revoke()
        groups = progress.controller_groups()
        groups.update(
            info["pid"]
            for name, info in known.items()
            if name != "safety" and info["pid"]
        )
        progress.stop_groups(groups)
        # Never rely on RPC to shut down a stalled Supervisor.
        try:
            os.kill(supervisor_pid, signal.SIGTERM)
            os.kill(supervisor_pid, signal.SIGCONT)
            time.sleep(progress.KILL_GRACE)
            os.kill(supervisor_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
