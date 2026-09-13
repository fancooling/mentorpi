"""
Safe keyboard teleoperation node for Ubuntu Tank.

Maps W/A/S/D to bounded Twist velocity commands published on /controller/cmd_vel.
Enforces renewable short motion leases (< 250 ms) to guarantee automatic stop
after key repeat ceases or input is lost, within the configured lease (or on
pause, terminal focus loss, signal, or exception).
"""

import os
import select
import signal
import sys
import time
from typing import Optional

if os.name != "nt":
    import tty
    import termios

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist

from ubuntu_tank_teleop.lease import TeleopLeaseManager

BANNER = """
------------------------------------------------------
MentorPi Safe Keyboard Teleoperation
------------------------------------------------------
  W : Forward
  S : Reverse
  A : Turn Left
  D : Turn Right
  Space : Immediate Stop
  Ctrl-C : Quit and Stop

Safety: Renewable 150 ms command lease.
Auto-stops within 150 ms when key repeat stops or pauses.
------------------------------------------------------
"""


class TeleopKeyNode(Node):
    """ROS 2 Node publishing lease-bounded Twist commands from keyboard input."""

    def __init__(self):
        super().__init__("teleop_key", start_parameter_services=False)

        self.declare_parameter("linear_vel", 0.2)
        self.declare_parameter("angular_vel", 0.5)
        self.declare_parameter("lease_duration_sec", 0.150)
        self.declare_parameter("publish_rate_hz", 20.0)

        lin_vel = self.get_parameter("linear_vel").value
        ang_vel = self.get_parameter("angular_vel").value
        lease_sec = self.get_parameter("lease_duration_sec").value
        rate_hz = self.get_parameter("publish_rate_hz").value

        self.publish_rate_hz = float(rate_hz) if rate_hz is not None else 20.0

        self.lease_mgr = TeleopLeaseManager(
            linear_vel=lin_vel, angular_vel=ang_vel, lease_duration_sec=lease_sec
        )

        self.cmd_pub = self.create_publisher(Twist, "/controller/cmd_vel", 10)

    def publish_twist(self, linear_x: float, angular_z: float):
        msg = Twist()
        msg.linear.x = float(linear_x)
        msg.angular.z = float(angular_z)
        self.cmd_pub.publish(msg)

    def publish_zero(self, count: int = 3):
        for _ in range(count):
            self.publish_twist(0.0, 0.0)


def main(args=None):
    rclpy.init(args=args)
    node = TeleopKeyNode()

    input_file = None
    input_fd = None
    opened_tty = False
    old_settings = None

    if os.name != "nt":
        if sys.stdin.isatty():
            input_file = sys.stdin
            input_fd = sys.stdin.fileno()
        else:
            try:
                input_file = open("/dev/tty", "r")
                input_fd = input_file.fileno()
                opened_tty = True
            except (OSError, IOError):
                input_file = None
                input_fd = None

        if input_fd is not None:
            try:
                old_settings = termios.tcgetattr(input_fd)
            except Exception:
                old_settings = None

    running = True

    def signal_handler(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(BANNER)
    if input_fd is None:
        sys.stderr.write(
            "Note: Interactive terminal (TTY) not detected on stdin or /dev/tty.\n"
            "Keyboard drive commands require an interactive terminal (e.g. './deploy.sh teleop').\n"
        )

    try:
        if old_settings is not None and input_fd is not None:
            tty.setraw(input_fd)

        loop_period = 1.0 / max(node.publish_rate_hz, 1.0)
        while running and rclpy.ok():
            now_mono = time.monotonic()

            # Poll input descriptor if interactive tty is available
            if old_settings is not None and input_fd is not None:
                rlist, _, _ = select.select([input_fd], [], [], loop_period)
                if rlist:
                    char = os.read(input_fd, 1).decode("utf-8", errors="ignore")
                    if char == "\x03":  # Ctrl-C
                        break
                    node.lease_mgr.process_key(char, now_mono)
            else:
                time.sleep(loop_period)

            # Check active lease and publish current velocity
            lin_x, ang_z = node.lease_mgr.get_velocities(time.monotonic())
            node.publish_twist(lin_x, ang_z)

            # Spin ROS callbacks non-blockingly
            rclpy.spin_once(node, timeout_sec=0.0)

    except Exception as exc:
        sys.stderr.write(f"\nTeleop error: {exc}\n")
    finally:
        node.publish_zero(count=3)
        if old_settings is not None and input_fd is not None:
            try:
                termios.tcsetattr(input_fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass
        if opened_tty and input_file is not None:
            try:
                input_file.close()
            except Exception:
                pass
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        print("\nTeleop exited. Robot commanded to stop.")


if __name__ == "__main__":
    main()
