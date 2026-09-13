#!/usr/bin/env python3
"""
bench_client.py - Dedicated bench testing and operator client for Milestone 6 acceptance.

Runs under enclave /ubuntu_tank/operator with node name 'operator_client'
to satisfy least-privilege SROS2 DDS security permissions.
Provides:
- Guard arm / disarm service requests (/ubuntu_tank_safety/set_arm)
- Bounded cmd_vel velocity publishing (/controller/cmd_vel)
- Guard state and armed status monitoring (/ubuntu_tank_safety/state, /ubuntu_tank_safety/armed)
- Finite motion execution and stop latency measurements
"""

import math
import sys
import time
from typing import Dict, List, Optional, Tuple

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from geometry_msgs.msg import Twist
    from std_msgs.msg import Bool
    from std_srvs.srv import SetBool
except ImportError:
    rclpy = None
    Parameter = None
    DurabilityPolicy = None
    QoSProfile = None
    ReliabilityPolicy = None

    class _FallbackNode:
        def __init__(self, *args, context=None, **kwargs):
            self._fallback_context = context

        @property
        def context(self):
            return self._fallback_context

        def destroy_node(self):
            pass

    Node = _FallbackNode

    class _Vector3:
        def __init__(self):
            self.x = 0.0
            self.y = 0.0
            self.z = 0.0

    class Twist:
        def __init__(self):
            self.linear = _Vector3()
            self.angular = _Vector3()

    class Bool:
        def __init__(self, data=False):
            self.data = data

    class _SetBool:
        class Request:
            def __init__(self):
                self.data = False

        class Response:
            def __init__(self):
                self.success = False
                self.message = ""

    SetBool = _SetBool

SingleThreadedExecutor = None
if rclpy is not None:
    try:
        from rclpy.executors import SingleThreadedExecutor as _STE

        SingleThreadedExecutor = _STE
    except (ImportError, AttributeError):
        SingleThreadedExecutor = getattr(
            getattr(rclpy, "executors", None), "SingleThreadedExecutor", None
        )


class BenchClientNode(Node):
    """Client node for executing bounded bench motions and measuring stop latencies."""

    def __init__(self, node_name: str = "operator_client", context=None):
        overrides = []
        if Parameter is not None:
            overrides.append(
                Parameter("start_type_description_service", Parameter.Type.BOOL, False)
            )

        self.guard_state: Optional[bool] = None
        self.guard_armed: Optional[bool] = None
        self._last_state_time: Optional[float] = None
        self._fallback_context = context
        self._executor = None

        if rclpy is not None:
            kwargs = {
                "start_parameter_services": False,
                "parameter_overrides": overrides,
            }
            if context is not None:
                try:
                    super().__init__(node_name, context=context, **kwargs)
                except TypeError:
                    super().__init__(node_name, **kwargs)
            else:
                super().__init__(node_name, **kwargs)

            if SingleThreadedExecutor is not None:
                try:
                    ctx = getattr(self, "context", None) or self._fallback_context
                    self._executor = SingleThreadedExecutor(context=ctx)
                    self._executor.add_node(self)
                except Exception:
                    self._executor = None

            # Arm/disarm service client
            self.arm_client = self.create_client(SetBool, "/ubuntu_tank_safety/set_arm")

            # Velocity command publisher
            self.cmd_vel_pub = self.create_publisher(Twist, "/controller/cmd_vel", 1)

            # Transient-local guard status subscriptions
            if QoSProfile and DurabilityPolicy and ReliabilityPolicy:
                transient_qos = QoSProfile(
                    depth=1,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                    reliability=ReliabilityPolicy.RELIABLE,
                )
            else:
                transient_qos = 1

            if Bool is not None:
                self.sub_state = self.create_subscription(
                    Bool, "/ubuntu_tank_safety/state", self._state_cb, transient_qos
                )
                self.sub_armed = self.create_subscription(
                    Bool, "/ubuntu_tank_safety/armed", self._armed_cb, transient_qos
                )
        else:
            try:
                super().__init__(node_name, context=context)
            except TypeError:
                super().__init__()
            self.arm_client = None
            self.cmd_vel_pub = None
            self.sub_state = None
            self.sub_armed = None

    def _state_cb(self, msg):
        self.guard_state = msg.data
        self._last_state_time = time.monotonic()

    def _armed_cb(self, msg):
        self.guard_armed = msg.data
        self._last_state_time = time.monotonic()

    def _is_ok(self) -> bool:
        ctx = getattr(self, "context", None) or self._fallback_context
        if ctx is not None:
            try:
                return ctx.ok()
            except Exception:
                pass
        if rclpy is not None:
            try:
                return rclpy.ok()
            except Exception:
                pass
        return True

    def _spin_once(self, timeout_sec: float = 0.1):
        if self._executor is not None:
            try:
                self._executor.spin_once(timeout_sec=timeout_sec)
                return
            except Exception:
                pass
        if rclpy is not None and rclpy.ok():
            rclpy.spin_once(self, timeout_sec=timeout_sec)

    def destroy_node(self):
        if self._executor is not None:
            try:
                self._executor.remove_node(self)
            except Exception:
                pass
            try:
                self._executor.shutdown()
            except Exception:
                pass
            self._executor = None
        if rclpy is not None and hasattr(super(), "destroy_node"):
            super().destroy_node()

    def call_set_arm(self, arm: bool, timeout_sec: float = 5.0) -> Tuple[bool, str]:
        """Send arm/disarm request to guard node and return (success, message)."""
        if self.arm_client is None:
            return False, "ROS client not initialized"

        start_time = time.monotonic()
        while not self.arm_client.wait_for_service(timeout_sec=0.2):
            if time.monotonic() - start_time >= timeout_sec:
                return (
                    False,
                    f"Timed out waiting for /ubuntu_tank_safety/set_arm after {timeout_sec:.1f}s",
                )
            if not self._is_ok():
                return False, "ROS context was shut down"

        req = SetBool.Request()
        req.data = arm
        future = self.arm_client.call_async(req)

        while self._is_ok():
            self._spin_once(timeout_sec=0.1)
            if future.done():
                try:
                    res = future.result()
                    return res.success, res.message
                except Exception as exc:
                    return False, f"Service call exception: {exc}"
            if time.monotonic() - start_time >= timeout_sec:
                return (
                    False,
                    f"Timed out waiting for set_arm response after {timeout_sec:.1f}s",
                )

        return False, "ROS context stopped before receiving response"

    def publish_cmd_vel(self, linear_x: float = 0.0, angular_z: float = 0.0) -> bool:
        """Publish a single Twist velocity command on /controller/cmd_vel."""
        if self.cmd_vel_pub is None or Twist is None:
            return False

        msg = Twist()
        msg.linear.x = float(linear_x)
        msg.linear.y = 0.0
        msg.linear.z = 0.0
        msg.angular.x = 0.0
        msg.angular.y = 0.0
        msg.angular.z = float(angular_z)
        self.cmd_vel_pub.publish(msg)
        return True

    def send_stop(self, count: int = 4) -> bool:
        """Send repeated zero velocity commands."""
        for _ in range(max(1, count)):
            if not self.publish_cmd_vel(0.0, 0.0):
                return False
            if self._is_ok():
                self._spin_once(timeout_sec=0.01)
        return True

    def run_motion_burst(
        self,
        linear_x: float,
        angular_z: float,
        duration_sec: float = 1.0,
        rate_hz: float = 20.0,
    ) -> bool:
        """
        Execute a finite, bounded motion burst, strictly self-terminating in a stop.
        """
        if duration_sec <= 0.0 or duration_sec > 5.0:
            raise ValueError(
                f"duration_sec must be between 0.0 and 5.0s, got {duration_sec}"
            )
        if rate_hz <= 0.0 or rate_hz > 100.0:
            raise ValueError(f"rate_hz must be between 0.0 and 100.0 Hz, got {rate_hz}")

        interval = 1.0 / rate_hz
        start_time = time.monotonic()
        try:
            while time.monotonic() - start_time < duration_sec:
                if not self._is_ok():
                    return False
                if self.guard_armed is False or not self.publish_cmd_vel(
                    linear_x, angular_z
                ):
                    return False
                if self._is_ok():
                    self._spin_once(timeout_sec=interval)
                else:
                    time.sleep(interval)
        finally:
            self.send_stop(count=4)

        return True

    def reset_state(self):
        """Discard samples before requesting a new arm/disarm transition."""
        self.guard_state = None
        self.guard_armed = None

    def wait_for_state(
        self, timeout_sec: float = 3.0, expected_armed: Optional[bool] = None
    ) -> Dict[str, Optional[bool]]:
        """Wait for fresh guard samples, optionally matching an expected arming state.

        Call reset_state before requesting a transition; callbacks dispatched
        while waiting for its service response must remain available here.
        On timeout return the samples received so far.
        """
        start = time.monotonic()
        while self._is_ok():
            self._spin_once(timeout_sec=0.05)
            if (
                self.guard_state is not None
                and self.guard_armed is not None
                and (expected_armed is None or self.guard_armed == expected_armed)
            ):
                break
            if time.monotonic() - start >= timeout_sec:
                break
        return {"guard_state": self.guard_state, "guard_armed": self.guard_armed}


def main(args=None):
    """Entry point for bench_client console script."""
    target_args = sys.argv[1:] if args is None else args
    if not target_args or "-h" in target_args or "--help" in target_args:
        print(
            "Usage: bench_client [--arm | --disarm | --stop | --motion <forward|reverse|left|right>]"
        )
        return 0

    if rclpy is None:
        sys.stderr.write("ERROR: rclpy is required to run bench_client.\n")
        return 1

    rclpy.init(args=args)
    node = BenchClientNode()
    try:
        if "--arm" in target_args:
            success, msg = node.call_set_arm(True)
            print(f"Arm result: success={success}, msg={msg}")
            return 0 if success else 1
        elif "--disarm" in target_args:
            success, msg = node.call_set_arm(False)
            print(f"Disarm result: success={success}, msg={msg}")
            return 0 if success else 1
        elif "--stop" in target_args:
            node.send_stop(count=4)
            print("Stop commands sent.")
            return 0
        elif "--motion" in target_args:
            idx = target_args.index("--motion")
            if idx + 1 >= len(target_args):
                sys.stderr.write(
                    "ERROR: --motion requires motion name (forward, reverse, left, right)\n"
                )
                return 1
            motion = target_args[idx + 1].lower()
            motions = {
                "forward": (0.2, 0.0),
                "reverse": (-0.2, 0.0),
                "left": (0.0, 0.8),
                "right": (0.0, -0.8),
            }
            if motion not in motions:
                sys.stderr.write(
                    f"ERROR: Unknown motion '{motion}'. Valid: {list(motions.keys())}\n"
                )
                return 1
            lx, az = motions[motion]
            print(
                f"Executing {motion} burst (linear.x={lx}, angular.z={az}, duration=1.0s)..."
            )
            node.run_motion_burst(lx, az, duration_sec=1.0)
            print("Motion completed. Zero commands sent.")
            return 0
        else:
            sys.stderr.write(f"ERROR: Unrecognized arguments: {target_args}\n")
            return 1
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
