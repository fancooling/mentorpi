"""
ROS 2 node wrapping MotorGuard.

Provides:
- Input subscription: /ubuntu_tank_safety/motor_input (MotorsState)
- Output publication: /ros_robot_controller/set_motor_guarded (MotorsState)
- Arm/disarm service: /ubuntu_tank_safety/set_arm (std_srvs/srv/SetBool)
- State topics: /ubuntu_tank_safety/state, /ubuntu_tank_safety/armed (std_msgs/msg/Bool, transient-local)
- Non-ROS monotonic heartbeat emission for process supervision
"""

import os
import signal
import socket
import sys
import time
from typing import List, Tuple

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from std_msgs.msg import Bool
from std_srvs.srv import SetBool

try:
    from ros_robot_controller_msgs.msg import MotorsState, MotorState
except ImportError:
    MotorsState = None
    MotorState = None

from ubuntu_tank_safety.motor_guard import MotorGuard


class MotorGuardNode(Node):
    """ROS 2 node enforcing motor command validation and guarding."""

    def __init__(self):
        super().__init__("motor_guard")

        # Declare ROS parameters with conservative defaults
        self.declare_parameter("max_rps", 2.0)
        self.declare_parameter("timeout_sec", 0.250)
        self.declare_parameter("check_rate_hz", 50.0)
        self.declare_parameter("heartbeat_interval_sec", 0.200)

        max_rps = self.get_parameter("max_rps").value
        timeout_sec = self.get_parameter("timeout_sec").value
        check_rate_hz = self.get_parameter("check_rate_hz").value
        heartbeat_interval_sec = self.get_parameter("heartbeat_interval_sec").value

        # Heartbeat communication channels (inherited FD preferred, dedicated socket fallback)
        guard_fd_str = os.environ.get(
            "UBUNTU_TANK_GUARD_HEARTBEAT_FD"
        ) or os.environ.get("UBUNTU_TANK_GUARD_PIPE_FD")
        self.guard_pipe_fd = (
            int(guard_fd_str) if guard_fd_str and guard_fd_str.isdigit() else None
        )
        self.guard_sock_path = os.environ.get(
            "UBUNTU_TANK_GUARD_SOCK", "/run/ubuntu_tank/guard_heartbeat.sock"
        )

        guard_pid_file = os.environ.get(
            "UBUNTU_TANK_GUARD_PID_FILE", "/run/ubuntu_tank/guard.pid"
        )
        if os.path.exists(os.path.dirname(guard_pid_file)):
            try:
                with open(guard_pid_file, "w", encoding="utf-8") as pf:
                    pf.write(str(os.getpid()))
            except Exception:
                pass

        self.guard = MotorGuard(max_rps=max_rps, timeout_sec=timeout_sec)

        # QoS for armed state: transient-local and reliable so late joiners receive it
        state_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )

        self.armed_pub = self.create_publisher(
            Bool, "/ubuntu_tank_safety/armed", state_qos
        )
        self.state_pub = self.create_publisher(
            Bool, "/ubuntu_tank_safety/state", state_qos
        )
        self.guarded_pub = self.create_publisher(
            MotorsState, "/ros_robot_controller/set_motor_guarded", 10
        )

        self.motor_sub = self.create_subscription(
            MotorsState, "/ubuntu_tank_safety/motor_input", self._on_motor_input, 10
        )

        self.arm_srv = self.create_service(
            SetBool, "/ubuntu_tank_safety/set_arm", self._handle_set_arm
        )

        # High-frequency watchdog timer for monotonic deadline checks
        timer_period = 1.0 / check_rate_hz
        self.watchdog_timer = self.create_timer(timer_period, self._on_watchdog_tick)

        # Heartbeat timer for supervisor
        self.heartbeat_timer = self.create_timer(
            heartbeat_interval_sec, self._emit_heartbeat
        )

        # Publish initial disarmed state
        self._publish_state()
        self.get_logger().info(
            "MotorGuard initialized. State: DISARMED (safety invariant)."
        )

    def _publish_state(self):
        msg = Bool()
        msg.data = self.guard.is_armed
        self.armed_pub.publish(msg)
        self.state_pub.publish(msg)

    def _publish_motor_command(self, motor_states: List[Tuple[int, float]]):
        if MotorsState is None or MotorState is None:
            return
        msg = MotorsState()
        msg.data = [MotorState(id=m_id, rps=float(rps)) for m_id, rps in motor_states]
        self.guarded_pub.publish(msg)

    def _publish_repeated_zero(self, count: int = 5):
        zero_cmd = self.guard.get_zero_command()
        for _ in range(count):
            self._publish_motor_command(zero_cmd)

    def _handle_set_arm(self, request, response):
        if request.data:
            success, message = self.guard.arm()
            response.success = success
            response.message = message
            self._publish_state()
            self.get_logger().warn("MotorGuard explicitly ARMED by operator.")
        else:
            success, message, zero_cmd = self.guard.disarm()
            self._publish_repeated_zero(count=5)
            response.success = success
            response.message = message
            self._publish_state()
            self.get_logger().info("MotorGuard explicitly DISARMED.")
        return response

    def _on_motor_input(self, msg: MotorsState):
        now_mono = time.monotonic()
        input_tuples = [(m.id, m.rps) for m in msg.data]

        fwd_cmd, fault_disarmed, reason = self.guard.handle_command(
            input_tuples, now_mono
        )
        if fault_disarmed:
            self.get_logger().error(
                f"MotorGuard safety fault: {reason}. Disarming and zeroing."
            )
            self._publish_repeated_zero(count=5)
            self._publish_state()
        elif fwd_cmd is not None:
            self._publish_motor_command(fwd_cmd)

    def _on_watchdog_tick(self):
        now_mono = time.monotonic()
        timed_out, zero_cmd = self.guard.check_timeout(now_mono)
        if timed_out:
            self.get_logger().warn(
                f"Motor command lease expired (> {self.guard.timeout_sec}s). Disarming."
            )
            self._publish_repeated_zero(count=5)
            self._publish_state()

    def _emit_heartbeat(self):
        """Emit non-ROS monotonic heartbeat via inherited FD or dedicated UNIX socket."""
        payload = f"{time.monotonic():.6f}\n".encode("utf-8")
        if self.guard_pipe_fd is not None:
            try:
                os.write(self.guard_pipe_fd, payload)
                return
            except Exception:
                pass

        if self.guard_sock_path and os.path.exists(self.guard_sock_path):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                    sock.sendto(payload, self.guard_sock_path)
            except Exception:
                pass

    def destroy_node(self):
        if getattr(self, "_is_destroyed", False):
            return
        self._is_destroyed = True
        try:
            self.get_logger().info(
                "MotorGuard shutting down. Publishing repeated zero commands."
            )
        except Exception:
            pass
        try:
            self._publish_repeated_zero(count=5)
        except Exception:
            pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = MotorGuardNode()

    def handle_sig(sig, frame):
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
