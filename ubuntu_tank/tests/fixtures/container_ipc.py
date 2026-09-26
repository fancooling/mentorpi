"""Serve installed IPC implementations across containers without ROS or hardware.

Used only by docker/ubuntu_tank/smoke.py. The lifecycle process boundary reports a
stopped controller and rejects mutations. This verifies image packaging, peer
credentials and shared-directory mounts, not DDS or real controller supervision.
"""

import os
import signal
import threading

from ubuntu_tank_operator.ipc_server import OperatorIpcServer
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_supervisor.lifecycle_service import LifecycleHelperService


def stopped_controller(command):
    """Answer only status; this fixture cannot start a controller."""
    if command[0] == "is-active":
        return 0, "inactive", ""
    return 1, "inactive", "Image IPC fixture forbids controller mutations"


def main():
    """Run the real socket services until container shutdown."""
    os.environ["UBUNTU_TANK_PRIVATE_DIR"] = "/tmp/private"
    ended = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: ended.set())
    operator = OperatorIpcServer(
        OperatorStateMachine(), socket_path="/smoke/ipc/operator.sock"
    )
    lifecycle = LifecycleHelperService(
        socket_path="/smoke/ipc/lifecycle.sock", process_runner=stopped_controller
    )
    operator.start()
    lifecycle.start()
    try:
        ended.wait()
    finally:
        lifecycle.stop()
        operator.stop()


if __name__ == "__main__":
    main()
