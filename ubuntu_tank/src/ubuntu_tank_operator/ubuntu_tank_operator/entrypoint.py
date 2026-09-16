"""
Entrypoint for the ubuntu_tank_operator agent daemon.

Configures loopback Fast DDS profile, enforces SROS2 least privilege,
registers signal handlers for safe zeroing and lock cleanup, and spins the node.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any

try:
    import rclpy
except ImportError:
    rclpy = None

from .agent_node import OperatorAgentNode


def configure_loopback_dds() -> None:
    """Ensure Fast DDS discovery contract and loopback profile are active."""
    if "FASTDDS_DEFAULT_PROFILES_FILE" not in os.environ:
        candidates = [
            "/opt/ubuntu_tank/current/config/fastdds/loopback.xml",
            os.path.join(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ),
                "config",
                "fastdds",
                "loopback.xml",
            ),
        ]
        for cand in candidates:
            if os.path.isfile(cand):
                os.environ["FASTDDS_DEFAULT_PROFILES_FILE"] = os.path.realpath(cand)
                os.environ.setdefault("RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
                os.environ.setdefault("ROS_DOMAIN_ID", "0")
                os.environ.setdefault("ROS_LOCALHOST_ONLY", "1")
                os.environ.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "SYSTEM_DEFAULT")
                break


def main(args: list[str] | None = None) -> int:
    """Run operator agent node and IPC server."""
    raw_args = sys.argv[1:] if args is None else args
    for arg in raw_args:
        if arg in ("-h", "--help"):
            print("Usage: ubuntu_tank_operator [--socket-path PATH] [--simulation]")
            return 0

    simulation = (
        os.environ.get("UBUNTU_TANK_SIMULATION") == "1" or "--simulation" in raw_args
    )

    socket_path = None
    target_args: list[str] = []
    i = 0
    while i < len(raw_args):
        arg = raw_args[i]
        if arg == "--socket-path" and i + 1 < len(raw_args):
            socket_path = raw_args[i + 1]
            i += 2
            continue
        if arg == "--simulation":
            i += 1
            continue
        target_args.append(arg)
        i += 1

    if rclpy is None and not simulation:
        sys.stderr.write(
            "FATAL: rclpy is not available and simulation mode is not enabled. "
            "Operator agent cannot run in production without ROS 2.\n"
        )
        return 1

    configure_loopback_dds()

    if rclpy is not None:
        rclpy.init(args=target_args)

    node = OperatorAgentNode(socket_path=socket_path)

    running = True

    def signal_handler(sig: int, frame: Any) -> None:
        nonlocal running
        running = False
        node.execute_stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if rclpy is not None:
            while running and rclpy.ok():
                rclpy.spin_once(node, timeout_sec=0.1)
        else:
            while running:
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        node.execute_stop()
        node.destroy_node()
        if rclpy is not None and rclpy.ok():
            rclpy.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
