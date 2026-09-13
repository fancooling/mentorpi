#!/usr/bin/env python3
"""
operator_client.py - Dedicated operator CLI client for arm/disarm operations.

Runs under enclave /ubuntu_tank/operator.
Explicitly disables unused parameter services and type description services
to satisfy least-privilege SROS2 DDS security permissions.
"""

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
            print("Usage: operator_client [--arm | --disarm]")
            return 0

    if rclpy is None:
        sys.stderr.write("ERROR: rclpy is required to run operator_client.\n")
        return 1

    arm = None
    for arg in target_args:
        if arg in ("--arm", "-a", "true", "True", "1"):
            arm = True
        elif arg in ("--disarm", "-d", "false", "False", "0"):
            arm = False

    if arm is None:
        sys.stderr.write("ERROR: Must specify --arm or --disarm.\n")
        return 1

    action_str = "arm" if arm else "disarm"
    rclpy.init(args=args)
    node = OperatorClientNode()
    try:
        success, message = node.call_set_arm(arm)
        if success:
            print(f"PASS: Guard successfully set to {action_str}ed ({message}).")
            return 0
        else:
            sys.stderr.write(f"FAIL: Guard {action_str} request failed: {message}\n")
            return 1
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
