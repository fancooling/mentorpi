"""
Operator Agent ROS 2 Node for MentorPi Pi 5 Web Control.

Runs under SROS2 enclave /ubuntu_tank/operator as the sole authorized publisher
of /controller/cmd_vel and client of /ubuntu_tank_safety/set_arm.
Owns:
1. OperatorStateMachine and lease tracking.
2. OperatorIpcServer managing Unix domain socket requests.
3. 20 Hz periodic /controller/cmd_vel publishing with fail-closed zeroing.
4. Immediate zero publishing on arm confirmation within 250 ms first-command deadline.
5. Telemetry ingestion from battery, guard state, and delivery observations.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from typing import Any

try:
    import rclpy
    from geometry_msgs.msg import Twist
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from std_msgs.msg import Bool, String, UInt16
    from std_srvs.srv import SetBool
except ImportError:
    rclpy = None
    Node = object
    Parameter = None
    DurabilityPolicy = None
    QoSProfile = None
    ReliabilityPolicy = None
    Twist = None
    Bool = None
    UInt16 = None
    String = None
    SetBool = None

from ubuntu_tank_protocol.constants import (
    DEFAULT_WEB_ANGULAR_SPEED,
    DEFAULT_WEB_LINEAR_SPEED,
    FIRST_COMMAND_DEADLINE_NS,
    LEASE_CHECK_INTERVAL_SEC,
)
from ubuntu_tank_protocol.enums import OperatorState, WebControlErrorCode

from .authority_lock import acquire_authority_lock, release_authority_lock
from .ipc_server import OperatorIpcServer
from .state_machine import OperatorStateMachine

logger = logging.getLogger(__name__)


class OperatorAgentNode(Node):
    """ROS 2 Node providing central operator authority, leases, and command arbitration."""

    def __init__(
        self,
        node_name: str = "operator_agent",
        socket_path: str | None = None,
        allowed_uids: list[int] | None = None,
        release_id: str = "current",
        linear_speed_cap: float = DEFAULT_WEB_LINEAR_SPEED,
        angular_speed_cap: float = DEFAULT_WEB_ANGULAR_SPEED,
        context: Any = None,
    ) -> None:
        self.release_id = release_id
        self._fallback_context = context
        self._observations: collections.deque = collections.deque(maxlen=1000)
        self._lock = threading.RLock()
        self.ipc_server = None

        # Initialize deterministic state machine
        self.state_machine = OperatorStateMachine(
            release_id=release_id,
            linear_speed_cap=linear_speed_cap,
            angular_speed_cap=angular_speed_cap,
            lock=self._lock,
        )

        # Wire disarm callback from state machine to physical ROS stop
        self.state_machine.on_disarm_required = self._on_state_machine_disarm_required

        # Exclude direct ROS producers before constructing any ROS publisher.
        self._lock_fd, self._lock_file = acquire_authority_lock(socket_path=socket_path)

        # SROS2 stripped parameter services overrides
        overrides = []
        if Parameter is not None:
            overrides.append(
                Parameter("start_type_description_service", Parameter.Type.BOOL, False)
            )

        if rclpy is not None:
            kwargs: dict[str, Any] = {
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

            # 1. Exclusive publisher to /controller/cmd_vel
            if Twist is not None:
                self.cmd_vel_pub = self.create_publisher(
                    Twist, "/controller/cmd_vel", 10
                )
            else:
                self.cmd_vel_pub = None

            # 2. Sole client for guard arming /ubuntu_tank_safety/set_arm
            if SetBool is not None:
                self.arm_client = self.create_client(
                    SetBool, "/ubuntu_tank_safety/set_arm"
                )
            else:
                self.arm_client = None

            # 3. Subscriptions for safety, battery, and delivery observations
            if QoSProfile and DurabilityPolicy and ReliabilityPolicy:
                transient_qos = QoSProfile(
                    depth=1,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                    reliability=ReliabilityPolicy.RELIABLE,
                )
            else:
                transient_qos = 1

            if Bool is not None:
                self.sub_guard_state = self.create_subscription(
                    Bool,
                    "/ubuntu_tank_safety/state",
                    self._guard_state_cb,
                    transient_qos,
                )
                self.sub_guard_armed = self.create_subscription(
                    Bool,
                    "/ubuntu_tank_safety/armed",
                    self._guard_armed_cb,
                    transient_qos,
                )
            else:
                self.sub_guard_state = None
                self.sub_guard_armed = None

            if UInt16 is not None:
                self.sub_battery = self.create_subscription(
                    UInt16, "/ros_robot_controller/battery", self._battery_cb, 10
                )
            else:
                self.sub_battery = None

            if String is not None:
                self.sub_obs = self.create_subscription(
                    String, "/ubuntu_tank/delivery_observation", self._obs_cb, 50
                )
            else:
                self.sub_obs = None

            # 20 ms Periodic Lease & Publishing Timer (50 Hz tick)
            self._timer = self.create_timer(LEASE_CHECK_INTERVAL_SEC, self._timer_tick)
        else:
            self.cmd_vel_pub = None
            self.arm_client = None
            self.sub_guard_state = None
            self.sub_guard_armed = None
            self.sub_battery = None
            self.sub_obs = None
            self._timer = None

        self._guard_liveness_override = None

        # Start Operator IPC Server
        self.ipc_server = OperatorIpcServer(
            state_machine=self.state_machine,
            socket_path=socket_path,
            allowed_uids=allowed_uids,
            arm_callback=self.execute_arm,
            stop_callback=self.execute_stop,
            observations_callback=self.get_observations,
            reset_observations_callback=self.reset_observations,
            lock=self._lock,
        )
        self.ipc_server.start()

    def _on_state_machine_disarm_required(self) -> None:
        """Invoked when state machine safety watchdog requires emergency disarm."""
        self.publish_zero(count=3)
        self._call_set_arm_async(False)

    def _guard_state_cb(self, msg: Any) -> None:
        now_ns = time.monotonic_ns()
        with self._lock:
            self.state_machine.telemetry.guard_monotonic_ns = now_ns

    def _guard_armed_cb(self, msg: Any) -> None:
        now_ns = time.monotonic_ns()
        val = bool(msg.data)
        with self._lock:
            self.state_machine.update_guard_telemetry(val, now_ns)

    def _battery_cb(self, msg: Any) -> None:
        """Process battery telemetry from ros_robot_controller (UInt16 mV)."""
        now_ns = time.monotonic_ns()
        try:
            mv = float(msg.data)
            volts = mv / 1000.0
            with self._lock:
                self.state_machine.update_battery_telemetry(volts, now_ns)
        except Exception as exc:
            logger.warning("Failed to parse battery telemetry: %s", exc)

    def _obs_cb(self, msg: Any) -> None:
        now_ns = time.monotonic_ns()
        try:
            data = json.loads(msg.data)
            data["_rx_mono_ns"] = now_ns
            with self._lock:
                self._observations.append(data)
                self.state_machine.telemetry.delivery_monotonic_ns = now_ns
                if data.get("node") == "motor_guard":
                    self.state_machine.telemetry.guard_monotonic_ns = now_ns
                    if "is_armed" in data:
                        self.state_machine.update_guard_telemetry(
                            bool(data["is_armed"]), now_ns
                        )
        except Exception:
            pass

    def _timer_tick(self) -> None:
        """Periodic safety loop checking lease expiry and publishing /controller/cmd_vel."""
        with self._lock:
            now_ns = time.monotonic_ns()
            # In disarmed/idle states, check guard service readiness to observe liveness
            if self.state_machine.state in (
                OperatorState.NO_OWNER,
                OperatorState.OWNED_DISARMED,
            ):
                is_ready = False
                if self._guard_liveness_override is not None:
                    try:
                        is_ready = bool(self._guard_liveness_override())
                    except Exception:
                        is_ready = False
                elif self.arm_client is not None:
                    try:
                        is_ready = bool(self.arm_client.service_is_ready())
                    except Exception:
                        is_ready = False

                if is_ready:
                    self.state_machine.telemetry.guard_monotonic_ns = now_ns
                    if self.state_machine.telemetry.guard_armed is None:
                        self.state_machine.telemetry.guard_armed = False

            # Check monotonic lease expiry across driving and armed-idle states
            healthy, _err_code, fault = self.state_machine.check_deadlines(now_ns)
            if not healthy:
                logger.warning("Motion lease expired: %s", fault)
                self.publish_zero(count=2)
                if (
                    self.state_machine.telemetry
                    and self.state_machine.telemetry.guard_armed is True
                ):
                    self._call_set_arm_async(False)

            # If disarm is pending or compensating disarm required, ensure zeros are published
            if (
                self.state_machine.disarm_pending
                or self.state_machine.compensating_disarm_required
            ):
                self.publish_zero(count=1)
                self._call_set_arm_async(False)
                return

            # Publish velocity from current state
            if self.state_machine.state in (
                OperatorState.DRIVING,
                OperatorState.ARMED_IDLE,
            ):
                # Work after the first check may consume the remaining lease,
                # so revalidate immediately before any publication.
                publish_now_ns = time.monotonic_ns()
                healthy, _err_code, fault = self.state_machine.check_deadlines(
                    publish_now_ns
                )
                if not healthy:
                    logger.warning("Motion lease expired before publication: %s", fault)
                    self.publish_zero(count=2)
                    if self.state_machine.telemetry.guard_armed is True:
                        self._call_set_arm_async(False)
                    return
                vx, wz = self.state_machine.get_velocity_command()
                self._publish_twist(vx, wz)

    def _publish_twist(self, linear_x: float, angular_z: float) -> None:
        if self.cmd_vel_pub is None or Twist is None:
            return
        msg = Twist()
        msg.linear.x = float(linear_x)
        msg.angular.z = float(angular_z)
        self.cmd_vel_pub.publish(msg)

    def publish_zero(self, count: int = 3) -> None:
        """Publish immediate zeros to /controller/cmd_vel."""
        for _ in range(max(1, count)):
            self._publish_twist(0.0, 0.0)

    def _call_set_arm_async(self, arm: bool) -> None:
        """Non-blocking call to /ubuntu_tank_safety/set_arm."""
        if self.arm_client is None or SetBool is None:
            return
        if not self.arm_client.service_is_ready():
            return
        req = SetBool.Request()
        req.data = arm
        try:
            self.arm_client.call_async(req)
        except Exception as exc:
            logger.warning("Failed to invoke set_arm async: %s", exc)

    @staticmethod
    def _is_confirmed_zero_write(observation: dict[str, Any]) -> bool:
        """Return whether an observation proves a complete four-motor zero write."""
        if (
            observation.get("stage") != "bridge_write"
            or observation.get("success") is not True
        ):
            return False
        try:
            written = int(observation.get("bytes_written", 0))
            expected = int(observation.get("bytes_expected", 0))
        except (TypeError, ValueError):
            return False
        if expected <= 0 or written != expected:
            return False

        motors = observation.get("motors")
        if isinstance(motors, dict):
            pairs = motors.items()
        elif isinstance(motors, (list, tuple)):
            pairs = []
            for item in motors:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    return False
                pairs.append((item[0], item[1]))
        else:
            return False
        try:
            values = {int(motor_id): float(speed) for motor_id, speed in pairs}
        except (TypeError, ValueError):
            return False
        return set(values) >= {1, 2, 3, 4} and all(
            abs(values[motor_id]) <= 1e-4 for motor_id in (1, 2, 3, 4)
        )

    def _wait_for_downstream_zero_write(
        self, since_monotonic_ns: int, deadline_monotonic_ns: int
    ) -> bool:
        """Wait for a fresh successful bridge zero write until the arm deadline."""
        while time.monotonic_ns() <= deadline_monotonic_ns:
            with self._lock:
                if any(
                    observation.get("_rx_mono_ns", 0) >= since_monotonic_ns
                    and self._is_confirmed_zero_write(observation)
                    for observation in self._observations
                ):
                    return True
                if self.state_machine.state != OperatorState.ARMING:
                    return False
            time.sleep(0.005)
        return False

    def execute_arm(
        self, epoch: int, request_id: str, timeout_sec: float = 3.0
    ) -> tuple[bool, str | None, str | None]:
        """
        Execute arming transaction against ROS guard node.

        Immediately publishes fresh zero on /controller/cmd_vel within 250 ms first-command deadline!
        """
        now_ns = time.monotonic_ns()
        with self._lock:
            # Transition to ARMING if not already initiated by caller
            if self.state_machine.state != OperatorState.ARMING:
                ok, err, msg = self.state_machine.arm(
                    self.state_machine.owner_id or "", epoch, True, now_ns, request_id
                )
                if not ok:
                    return (
                        False,
                        err.value
                        if err
                        else WebControlErrorCode.OPERATION_FAILED.value,
                        msg,
                    )

        # If arm_client is not initialized (e.g. ROS disabled), complete transition locally
        if self.arm_client is None or SetBool is None:
            with self._lock:
                ok, err, msg = self.state_machine.confirm_armed(
                    guard_confirmed=True,
                    downstream_zero_confirmed=True,
                    current_monotonic_ns=time.monotonic_ns(),
                    epoch=epoch,
                    request_id=request_id,
                )
            return (
                ok,
                err.value if err else None,
                "Armed successfully (simulation)" if ok else msg,
            )

        start_time = time.monotonic()
        while not self.arm_client.wait_for_service(timeout_sec=0.2):
            if time.monotonic() - start_time >= timeout_sec:
                with self._lock:
                    self.state_machine.stop(time.monotonic_ns())
                    self.state_machine.state = OperatorState.FAULT
                    self.state_machine.last_fault = (
                        "Timed out waiting for guard set_arm service"
                    )
                    self.state_machine.compensating_disarm_required = True
                self._call_set_arm_async(False)
                return (
                    False,
                    WebControlErrorCode.TIMEOUT.value,
                    "Timed out waiting for /ubuntu_tank_safety/set_arm",
                )

        req = SetBool.Request()
        req.data = True
        future = self.arm_client.call_async(req)

        # Spin context or poll future
        while not future.done():
            if time.monotonic() - start_time >= timeout_sec:
                with self._lock:
                    self.state_machine.stop(time.monotonic_ns())
                    self.state_machine.state = OperatorState.FAULT
                    self.state_machine.last_fault = (
                        "Timed out waiting for guard set_arm response"
                    )
                    self.state_machine.compensating_disarm_required = True
                self._call_set_arm_async(False)
                return (
                    False,
                    WebControlErrorCode.TIMEOUT.value,
                    "Timed out waiting for set_arm response",
                )
            time.sleep(0.01)

        try:
            res = future.result()
            if not res.success:
                with self._lock:
                    self.state_machine.stop(time.monotonic_ns())
                    self.state_machine.state = OperatorState.FAULT
                    self.state_machine.last_fault = f"Guard rejected arm: {res.message}"
                    self.state_machine.compensating_disarm_required = True
                self._call_set_arm_async(False)
                return False, WebControlErrorCode.PREFLIGHT_FAILED.value, res.message

            # Publish a fresh zero, then require correlated evidence that the
            # complete four-motor zero reached the bridge write edge.
            zero_start_ns = time.monotonic_ns()
            self.publish_zero(count=3)

            with self._lock:
                arming_start_ns = self.state_machine.arming_start_monotonic_ns
            deadline_ns = (
                arming_start_ns + FIRST_COMMAND_DEADLINE_NS
                if arming_start_ns is not None
                else zero_start_ns
            )
            downstream_zero_confirmed = self._wait_for_downstream_zero_write(
                zero_start_ns, deadline_ns
            )

            # Confirm transition to ARMED_IDLE
            with self._lock:
                ok, err, msg = self.state_machine.confirm_armed(
                    guard_confirmed=True,
                    downstream_zero_confirmed=downstream_zero_confirmed,
                    current_monotonic_ns=time.monotonic_ns(),
                    epoch=epoch,
                    request_id=request_id,
                )
            if not ok:
                self._call_set_arm_async(False)
                return (
                    False,
                    err.value if err else WebControlErrorCode.OPERATION_FAILED.value,
                    msg,
                )
            return True, None, res.message or msg

        except Exception as exc:
            with self._lock:
                self.state_machine.stop(time.monotonic_ns())
                self.state_machine.state = OperatorState.FAULT
                self.state_machine.last_fault = f"Arm exception: {exc}"
                self.state_machine.compensating_disarm_required = True
            self._call_set_arm_async(False)
            return False, WebControlErrorCode.OPERATION_FAILED.value, str(exc)

    def execute_stop(self) -> None:
        """Immediate stop and disarm priority."""
        self.publish_zero(count=4)
        self._call_set_arm_async(False)

    def get_observations(
        self, since_mono_ns: int | None = None
    ) -> list[dict[str, Any]]:
        """Return delivery observations collected by the agent."""
        with self._lock:
            if since_mono_ns is None:
                return list(self._observations)
            return [
                obs
                for obs in self._observations
                if obs.get("_rx_mono_ns", 0) >= since_mono_ns
            ]

    def reset_observations(self) -> int:
        """Clear delivery observations and return reset monotonic time in nanoseconds."""
        now_ns = time.monotonic_ns()
        with self._lock:
            self._observations.clear()
        return now_ns

    def destroy_node(self) -> None:
        """Cleanly stop IPC server, release lock, and destroy ROS node."""
        if self.ipc_server is not None:
            self.ipc_server.stop()
            self.ipc_server = None
        self.publish_zero(count=3)
        self._call_set_arm_async(False)
        try:
            if hasattr(super(), "destroy_node"):
                super().destroy_node()
        finally:
            # Keep authority until every ROS publisher/client is destroyed.
            if hasattr(self, "_lock_fd") and self._lock_fd is not None:
                release_authority_lock(self._lock_fd)
                self._lock_fd = None
