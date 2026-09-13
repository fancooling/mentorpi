#!/usr/bin/env python3
"""
status_client.py - Dedicated status CLI client for reading guard and battery status.

Runs under enclave /ubuntu_tank/status.
Explicitly disables unused parameter services and type description services
to satisfy least-privilege SROS2 DDS security permissions.
Strictly read-only: creates subscriptions only, zero publishers, zero service clients.
"""

import sys
import time

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
    from std_msgs.msg import Bool, UInt16
except ImportError:
    rclpy = None
    Parameter = None
    QoSProfile = None
    DurabilityPolicy = None
    ReliabilityPolicy = None
    Bool = None
    UInt16 = None

    class _FallbackNode:
        def __init__(self, *args, context=None, **kwargs):
            self._fallback_context = context

        @property
        def context(self):
            return self._fallback_context

        def destroy_node(self):
            pass

    Node = _FallbackNode

SingleThreadedExecutor = None
if rclpy is not None:
    try:
        from rclpy.executors import SingleThreadedExecutor as _STE

        SingleThreadedExecutor = _STE
    except (ImportError, AttributeError):
        SingleThreadedExecutor = getattr(
            getattr(rclpy, "executors", None), "SingleThreadedExecutor", None
        )


class StatusClientNode(Node):
    """Client node for reading guard state and telemetry with unneeded services stripped."""

    def __init__(self, node_name: str = "status_client", context=None):
        overrides = []
        if Parameter is not None:
            overrides.append(
                Parameter("start_type_description_service", Parameter.Type.BOOL, False)
            )

        self.guard_state = None
        self.guard_armed = None
        self.battery_mv = None
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

            if QoSProfile and DurabilityPolicy and ReliabilityPolicy:
                transient_qos = QoSProfile(
                    depth=1,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                    reliability=ReliabilityPolicy.RELIABLE,
                )
                standard_qos = QoSProfile(
                    depth=1, reliability=ReliabilityPolicy.RELIABLE
                )
            else:
                transient_qos = 1
                standard_qos = 1

            if Bool is not None:
                self.sub_state = self.create_subscription(
                    Bool, "/ubuntu_tank_safety/state", self._state_cb, transient_qos
                )
                self.sub_armed = self.create_subscription(
                    Bool, "/ubuntu_tank_safety/armed", self._armed_cb, transient_qos
                )
            if UInt16 is not None:
                self.sub_battery = self.create_subscription(
                    UInt16,
                    "/ros_robot_controller/battery",
                    self._battery_cb,
                    standard_qos,
                )
        else:
            try:
                super().__init__(node_name, context=context)
            except TypeError:
                super().__init__()
            self.sub_state = None
            self.sub_armed = None
            self.sub_battery = None

    def _state_cb(self, msg):
        self.guard_state = msg.data

    def _armed_cb(self, msg):
        self.guard_armed = msg.data

    def _battery_cb(self, msg):
        self.battery_mv = msg.data

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

    def collect_status(self, timeout_sec: float = 3.0) -> dict:
        """Wait and collect guard status and battery telemetry until timeout."""
        if rclpy is None:
            return {"guard_state": None, "guard_armed": None, "battery_mv": None}

        start_time = time.monotonic()
        while self._is_ok():
            self._spin_once(timeout_sec=0.1)
            if (
                self.guard_state is not None
                and self.guard_armed is not None
                and self.battery_mv is not None
            ):
                break
            if time.monotonic() - start_time >= timeout_sec:
                break

        return {
            "guard_state": self.guard_state,
            "guard_armed": self.guard_armed,
            "battery_mv": self.battery_mv,
        }


def main(args=None):
    """Entry point for status_client console script."""
    target_args = sys.argv[1:] if args is None else args
    for arg in target_args:
        if arg in ("-h", "--help"):
            print("Usage: status_client")
            return 0

    if rclpy is None:
        sys.stderr.write("ERROR: rclpy is required to run status_client.\n")
        return 1

    rclpy.init(args=args)
    node = StatusClientNode()
    try:
        status = node.collect_status(timeout_sec=3.0)
        if status["guard_state"] is None and status["guard_armed"] is None:
            sys.stderr.write("Guard state topic unavailable or not publishing.\n")
            return 1

        print("============================================================")
        print("MentorPi Tank Controller Status")
        print("============================================================")
        print(f"  Guard State: {'OK' if status['guard_state'] else 'STOP/FAULT'}")
        print(f"  Armed:       {'ARMED' if status['guard_armed'] else 'DISARMED'}")
        if status["battery_mv"] is not None:
            print(f"  Battery:     {status['battery_mv'] / 1000.0:.2f} V")
        print("============================================================")
        return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
