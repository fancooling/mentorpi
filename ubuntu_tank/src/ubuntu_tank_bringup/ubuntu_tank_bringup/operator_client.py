#!/usr/bin/env python3
"""
operator_client.py - Dedicated operator CLI client for arm/disarm operations.

Runs under enclave /ubuntu_tank/operator.
Explicitly disables unused parameter services and type description services
to satisfy least-privilege SROS2 DDS security permissions.
"""

import os
import sys
import time

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from std_srvs.srv import SetBool
except ImportError:
    rclpy = None
    Node = object
    Parameter = None
    SetBool = None


class OperatorClientNode(Node):
    """Client node for sending arm/disarm requests with unneeded services stripped."""

    def __init__(self, node_name: str = "operator_client"):
        overrides = []
        if Parameter is not None:
            overrides.append(
                Parameter("start_type_description_service", Parameter.Type.BOOL, False)
            )

        if rclpy is not None:
            super().__init__(
                node_name, start_parameter_services=False, parameter_overrides=overrides
            )
            self.client = self.create_client(SetBool, "/ubuntu_tank_safety/set_arm")
        else:
            self.client = None

    def call_set_arm(self, arm: bool, timeout_sec: float = 5.0) -> tuple[bool, str]:
        """Send arm/disarm request to guard node and return (success, message)."""
        if self.client is None:
            return False, "ROS client not initialized"

        start_time = time.monotonic()
        while not self.client.wait_for_service(timeout_sec=0.2):
            if time.monotonic() - start_time >= timeout_sec:
                return (
                    False,
                    f"Timed out waiting for service /ubuntu_tank_safety/set_arm after {timeout_sec:.1f}s",
                )
            if not rclpy.ok():
                return False, "ROS context was shut down"

        req = SetBool.Request()
        req.data = arm
        future = self.client.call_async(req)

        while rclpy.ok():
            rclpy.spin_once(self, timeout_sec=0.1)
            if future.done():
                try:
                    res = future.result()
                    return res.success, res.message
                except Exception as exc:
                    return False, f"Service call exception: {exc}"
            if time.monotonic() - start_time >= timeout_sec:
                return (
                    False,
                    f"Timed out waiting for /ubuntu_tank_safety/set_arm response after {timeout_sec:.1f}s",
                )

        return False, "ROS context stopped before receiving response"


def main(args=None):
    """Entry point for operator_client console script."""
    target_args = sys.argv[1:] if args is None else args
    for arg in target_args:
        if arg in ("-h", "--help"):
            print("Usage: operator_client [--arm | --disarm] [--direct-ros]")
            return 0

    arm = None
    direct = False
    for arg in target_args:
        if arg in ("--arm", "-a", "true", "True", "1"):
            arm = True
        elif arg in ("--disarm", "-d", "false", "False", "0"):
            arm = False
        elif arg in ("--direct", "--direct-ros"):
            direct = True

    if arm is None:
        sys.stderr.write("ERROR: Must specify --arm or --disarm.\n")
        return 1

    action_str = "arm" if arm else "disarm"

    # Route through shared Operator Agent IPC if not explicitly --direct
    if not direct:
        try:
            from ubuntu_tank_operator.ipc_client import OperatorIpcClient

            client = OperatorIpcClient()
            try:
                client.connect(timeout_sec=1.0)
                if arm:
                    op_id = f"cli_operator_{os.getpid()}"
                    acq_ok, epoch, acq_err, acq_msg = client.acquire(
                        op_id, timeout_sec=2.0
                    )
                    if not acq_ok or epoch is None:
                        sys.stderr.write(
                            f"FAIL: Guard arm request failed: {acq_msg} ({acq_err})\n"
                        )
                        return 1
                    arm_ok, arm_err, arm_msg = client.arm(
                        epoch, tracks_raised=True, timeout_sec=3.0
                    )
                    if arm_ok:
                        print(f"PASS: Guard successfully set to armed ({arm_msg}).")
                        return 0
                    else:
                        sys.stderr.write(
                            f"FAIL: Guard arm request failed: {arm_msg} ({arm_err})\n"
                        )
                        return 1
                else:
                    client.stop(timeout_sec=2.0)
                    print(f"PASS: Guard successfully set to disarmed.")
                    return 0
            except (ConnectionError, FileNotFoundError, OSError) as exc:
                sys.stderr.write(
                    f"ERROR: Operator agent daemon is not reachable: {exc}.\n"
                    "Use --direct-ros only for an explicitly isolated direct ROS session.\n"
                )
                return 1
            finally:
                client.close()
        except ImportError as exc:
            sys.stderr.write(
                f"ERROR: Operator Agent IPC client is unavailable: {exc}.\n"
            )
            return 1

    if rclpy is None:
        sys.stderr.write("ERROR: rclpy is required to run direct operator_client.\n")
        return 1

    if "FASTDDS_DEFAULT_PROFILES_FILE" not in os.environ:
        for cand in [
            "/opt/ubuntu_tank/current/config/fastdds/loopback.xml",
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "..",
                "..",
                "config",
                "fastdds",
                "loopback.xml",
            ),
        ]:
            if os.path.isfile(cand):
                os.environ["FASTDDS_DEFAULT_PROFILES_FILE"] = os.path.realpath(cand)
                os.environ.setdefault("RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
                os.environ.setdefault("ROS_DOMAIN_ID", "0")
                os.environ.setdefault("ROS_LOCALHOST_ONLY", "1")
                os.environ.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "SYSTEM_DEFAULT")
                break

    from ubuntu_tank_operator.authority_lock import (
        acquire_authority_lock,
        release_authority_lock,
    )

    try:
        authority_lock_fd, _ = acquire_authority_lock()
    except (OSError, RuntimeError) as exc:
        sys.stderr.write(
            f"ERROR: Could not acquire exclusive operator authority lock: {exc}.\n"
        )
        return 1

    node = None
    try:
        rclpy.init(args=args)
        node = OperatorClientNode()
        success, message = node.call_set_arm(arm)
        if success:
            print(f"PASS: Guard successfully set to {action_str}ed ({message}).")
            return 0
        else:
            sys.stderr.write(f"FAIL: Guard {action_str} request failed: {message}\n")
            return 1
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        release_authority_lock(authority_lock_fd)


if __name__ == "__main__":
    sys.exit(main())
