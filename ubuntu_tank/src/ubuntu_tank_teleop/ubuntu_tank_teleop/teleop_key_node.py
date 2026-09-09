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

if os.name != 'nt':
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
        super().__init__('teleop_key')

        self.declare_parameter('linear_vel', 0.2)
        self.declare_parameter('angular_vel', 0.5)
        self.declare_parameter('lease_duration_sec', 0.150)
        self.declare_parameter('publish_rate_hz', 20.0)

        lin_vel = self.get_parameter('linear_vel').value
        ang_vel = self.get_parameter('angular_vel').value
        lease_sec = self.get_parameter('lease_duration_sec').value

        self.lease_mgr = TeleopLeaseManager(
            linear_vel=lin_vel,
            angular_vel=ang_vel,
            lease_duration_sec=lease_sec
        )

        self.cmd_pub = self.create_publisher(Twist, '/controller/cmd_vel', 10)

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

    old_settings = None
    if os.name != 'nt' and sys.stdin.isatty():
        old_settings = termios.tcgetattr(sys.stdin)

    running = True

    def signal_handler(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(BANNER)

    try:
        if old_settings is not None:
            tty.setraw(sys.stdin.fileno())

        loop_period = 0.050  # 20 Hz
        while running and rclpy.ok():
            now_mono = time.monotonic()

            # Poll stdin if interactive tty
            if old_settings is not None:
                rlist, _, _ = select.select([sys.stdin], [], [], loop_period)
                if rlist:
                    char = sys.stdin.read(1)
                    if char == '\x03':  # Ctrl-C
                        break
                    node.lease_mgr.process_key(char, now_mono)

            # Check active lease and publish current velocity
            lin_x, ang_z = node.lease_mgr.get_velocities(time.monotonic())
            node.publish_twist(lin_x, ang_z)

            # Spin ROS callbacks non-blockingly
            rclpy.spin_once(node, timeout_sec=0.0)

    except Exception as exc:
        sys.stderr.write(f"\nTeleop error: {exc}\n")
    finally:
        node.publish_zero(count=3)
        if old_settings is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        print("\nTeleop exited. Robot commanded to stop.")


if __name__ == '__main__':
    main()
