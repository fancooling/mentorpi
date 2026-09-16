"""
Safe keyboard teleoperation node for Ubuntu Tank.

Maps W/A/S/D to bounded Twist velocity commands published on /controller/cmd_vel.
Enforces renewable short motion leases (< 250 ms) to guarantee automatic stop
after key repeat ceases or input is lost, within the configured lease (or on
pause, terminal focus loss, signal, or exception).

Adapted/refactored from Hiwonder MentorPi:

mentorpi/src/peripherals/peripherals/teleop_key_control.py.

Local adaptations: Replaced latched linear commands with 150 ms renewable leases;
periodic 20 Hz publishing; auto-zero on lease expiry/SIGINT/SIGTERM; removed unused
servo and camera code.
"""

import io
import math
import os
import select
import signal
import socket
import sys
import time
from typing import Optional

if os.name != "nt":
    import tty
    import termios

try:
    import rclpy
    from rclpy.node import Node
    from geometry_msgs.msg import Twist
except ImportError:
    rclpy = None
    Node = object
    Twist = None

from ubuntu_tank_teleop.lease import TeleopLeaseManager

BANNER = """
------------------------------------------------------
MentorPi Safe Keyboard Teleoperation
------------------------------------------------------
  W : Forward
  S : Reverse
  A : Turn Left
  D : Turn Right
  R : Arm (tracks raised acknowledged)
  Space : Immediate Stop
  Ctrl-C : Quit and Stop

Safety: Renewable 150 ms command lease.
Auto-stops within 150 ms when key repeat stops or pauses.
------------------------------------------------------
"""


class TeleopKeyNode(Node):
    """ROS 2 Node publishing lease-bounded Twist commands from keyboard input."""

    def __init__(
        self,
        linear_vel: Optional[float] = None,
        angular_vel: Optional[float] = None,
    ):
        if rclpy is not None:
            super().__init__("teleop_key", start_parameter_services=False)

            self.declare_parameter("linear_vel", 0.2)
            self.declare_parameter("angular_vel", 0.5)
            self.declare_parameter("lease_duration_sec", 0.150)
            self.declare_parameter("publish_rate_hz", 20.0)

            lin_vel = self.get_parameter("linear_vel").value
            ang_vel = self.get_parameter("angular_vel").value
            lease_sec = self.get_parameter("lease_duration_sec").value
            rate_hz = self.get_parameter("publish_rate_hz").value
        else:
            lin_vel = 0.2
            ang_vel = 0.5
            lease_sec = 0.150
            rate_hz = 20.0

        if linear_vel is not None:
            lin_vel = float(linear_vel)
        if angular_vel is not None:
            ang_vel = float(angular_vel)

        self.publish_rate_hz = float(rate_hz) if rate_hz is not None else 20.0

        self.lease_mgr = TeleopLeaseManager(
            linear_vel=lin_vel, angular_vel=ang_vel, lease_duration_sec=lease_sec
        )

        if rclpy is not None and Twist is not None:
            self.cmd_pub = self.create_publisher(Twist, "/controller/cmd_vel", 10)
        else:
            self.cmd_pub = None

    def publish_twist(self, linear_x: float, angular_z: float):
        if self.cmd_pub is not None and Twist is not None:
            msg = Twist()
            msg.linear.x = float(linear_x)
            msg.angular.z = float(angular_z)
            self.cmd_pub.publish(msg)

    def publish_zero(self, count: int = 3):
        for _ in range(count):
            self.publish_twist(0.0, 0.0)


def main(args=None):
    """Run keyboard control with host ROS parameters and explicit speed overrides.

    IPC uses host linear/angular parameters unless CLI speed flags override them;
    acquisition negotiates downward to agent caps. The keyboard input lease is
    bounded by both the configured duration and the agent's 150 ms maximum.
    Invalid numeric settings fail before connecting or arming.
    """
    import argparse

    target_args = sys.argv[1:] if args is None else args
    parser = argparse.ArgumentParser(description="MentorPi Safe Keyboard Teleoperation")
    parser.add_argument(
        "--ack-tracks-raised",
        action="store_true",
        help="Acknowledge tracks are raised and arm the robot upon start",
    )
    parser.add_argument(
        "--direct-ros",
        action="store_true",
        help="Explicitly select legacy direct ROS mode (mutually exclusive with agent operation)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help="Target linear operating speed in m/s (clamped to agent caps, default 0.20)",
    )
    parser.add_argument(
        "--angular-speed",
        type=float,
        default=None,
        help="Target angular operating speed in rad/s (clamped to agent caps, default 0.50)",
    )
    parsed, remaining = parser.parse_known_args(target_args)
    # deploy.sh supplies validated host settings as ROS parameter assignments.
    # Explicit CLI speed flags take precedence in both transport modes.
    settings = {}
    for index, argument in enumerate(remaining):
        if index and remaining[index - 1] in ("-p", "--param"):
            name, separator, value = argument.partition(":=")
            if separator and name in (
                "linear_vel",
                "angular_vel",
                "lease_duration_sec",
            ):
                try:
                    setting = float(value)
                    if not math.isfinite(setting) or setting <= 0:
                        raise ValueError("must be finite and positive")
                    settings[name] = setting
                except ValueError as exc:
                    parser.error(f"Invalid {name}: {exc}")
    if parsed.speed is None:
        parsed.speed = settings.get("linear_vel")
    if parsed.angular_speed is None:
        parsed.angular_speed = settings.get("angular_vel")
    keyboard_lease = min(settings.get("lease_duration_sec", 0.150), 0.150)
    for speed in (parsed.speed, parsed.angular_speed):
        if speed is not None and (not math.isfinite(speed) or speed <= 0):
            parser.error("Operating speeds must be finite and positive")

    ipc_client = None
    ipc_epoch = None
    is_armed = False
    node = None
    direct_lock_fd: Optional[int] = None

    if parsed.direct_ros:
        sock_path = os.environ.get(
            "UBUNTU_TANK_OPERATOR_SOCKET", "/run/ubuntu_tank/operator.sock"
        )

        # 1. Probe operator socket: direct mode is strictly forbidden if agent is active
        if os.path.exists(sock_path):
            test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                test_sock.connect(sock_path)
                test_sock.close()
                sys.stderr.write(
                    f"ERROR: Operator agent daemon is active at '{sock_path}'. "
                    "Direct ROS mode is strictly forbidden while the operator agent daemon is active.\n"
                )
                return 1
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                try:
                    test_sock.close()
                except OSError:
                    pass

        # 2. Acquire operator authority exclusion lock
        try:
            from ubuntu_tank_operator.authority_lock import acquire_authority_lock

            direct_lock_fd, lock_path = acquire_authority_lock(socket_path=sock_path)
        except (ImportError, OSError, RuntimeError) as exc:
            sys.stderr.write(
                f"ERROR: Could not acquire exclusive operator authority lock: {exc}. "
                "Direct ROS mode cannot proceed.\n"
            )
            return 1

        print("Explicitly selected direct ROS mode (bypassing Operator Agent IPC).")
        if rclpy is None:
            sys.stderr.write("ERROR: rclpy is required for direct ROS mode.\n")
            if direct_lock_fd is not None:
                from ubuntu_tank_operator.authority_lock import release_authority_lock

                release_authority_lock(direct_lock_fd)
            return 1
        rclpy.init(args=args)
        node = TeleopKeyNode(linear_vel=parsed.speed, angular_vel=parsed.angular_speed)
    else:
        # Route through shared Operator Agent IPC
        try:
            from ubuntu_tank_operator.ipc_client import OperatorIpcClient

            client = OperatorIpcClient()
            try:
                client.connect(timeout_sec=0.5)
            except (ConnectionError, FileNotFoundError, OSError) as exc:
                sys.stderr.write(
                    f"ERROR: Operator agent daemon is not reachable: {exc}.\n"
                    "Use --direct-ros to explicitly select direct ROS mode if agent is absent.\n"
                )
                return 1

            owner_id = f"cli_teleop_{os.getpid()}"
            ok, epoch, err, msg = client.acquire(
                owner_id,
                max_linear_speed=parsed.speed,
                max_angular_speed=parsed.angular_speed,
                timeout_sec=1.5,
            )
            if not ok or epoch is None:
                client.close()
                sys.stderr.write(
                    f"ERROR: Operator agent denied ownership: {msg} ({err}). Exiting.\n"
                )
                return 1  # FAIL CLOSED: Never fall back to direct publisher on ownership rejection!

            ipc_client = client
            ipc_epoch = epoch

            # If tracks raised was acknowledged on CLI, arm immediately within this persistent session
            if parsed.ack_tracks_raised:
                arm_ok, arm_err, arm_msg = ipc_client.arm(
                    epoch=ipc_epoch, tracks_raised=True, timeout_sec=3.0
                )
                if arm_ok:
                    is_armed = True
                    print(
                        f"PASS: Operator agent armed successfully ({arm_msg}) with tracks raised."
                    )
                else:
                    sys.stderr.write(
                        f"FAIL: Guard arming request failed: {arm_msg} ({arm_err}). Exiting.\n"
                    )
                    try:
                        ipc_client.release(ipc_epoch)
                        ipc_client.close()
                    except Exception:
                        pass
                    return 1
        except ImportError:
            sys.stderr.write(
                "ERROR: ubuntu_tank_operator is required when direct ROS mode is not selected.\n"
            )
            return 1

    input_file = None
    input_fd = None
    opened_tty = False
    old_settings = None

    if os.name != "nt":
        if hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            input_file = sys.stdin
            input_fd = sys.stdin.fileno()
        elif sys.stdin == sys.__stdin__:
            try:
                input_file = open("/dev/tty", "r")
                input_fd = input_file.fileno()
                opened_tty = True
            except (OSError, IOError):
                input_file = None
                input_fd = None
        else:
            input_file = sys.stdin
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
    if ipc_client is not None:
        status_str = (
            "ARMED (Ready to drive)"
            if is_armed
            else "DISARMED (Press 'r' to arm tracks-raised)"
        )
        print(
            f"Connected to Operator Agent IPC (exclusive ownership active). Status: {status_str}"
        )
    if input_fd is None:
        sys.stderr.write(
            "Note: Interactive terminal (TTY) not detected on stdin or /dev/tty.\n"
            "Keyboard drive commands require an interactive terminal (e.g. './deploy.sh teleop').\n"
        )

    seq = 0
    key_to_dir = {
        "w": "forward",
        "s": "reverse",
        "a": "spin_left",
        "d": "spin_right",
        " ": "stop",
    }
    last_key_time = 0.0
    current_key = None

    try:
        if old_settings is not None and input_fd is not None:
            tty.setraw(input_fd)

        loop_period = min(0.050, keyboard_lease / 2)
        while running and (
            ipc_client is not None or (rclpy is not None and rclpy.ok())
        ):
            now_mono = time.monotonic()

            # Poll input descriptor if interactive tty is available
            char = None
            if old_settings is not None and input_fd is not None:
                rlist, _, _ = select.select([input_fd], [], [], loop_period)
                if rlist:
                    char = os.read(input_fd, 1).decode("utf-8", errors="ignore").lower()
            else:
                # Fallback for non-interactive / piped stdin or test StringIO
                fd = None
                try:
                    if input_file is not None:
                        fd = input_file.fileno()
                except (io.UnsupportedOperation, AttributeError, OSError):
                    fd = None

                try:
                    if fd is not None:
                        rlist, _, _ = select.select([fd], [], [], loop_period)
                        if rlist:
                            raw = os.read(fd, 1)
                            if not raw:
                                break
                            char = raw.decode("utf-8", errors="ignore").lower()
                        else:
                            time.sleep(loop_period)
                    elif input_file is not None:
                        read_c = input_file.read(1)
                        if not read_c:
                            break
                        char = read_c.lower()
                        time.sleep(loop_period)
                    else:
                        time.sleep(loop_period)
                except Exception:
                    time.sleep(loop_period)

            if char:
                if char == "\x03" or char == "q":  # Ctrl-C or 'q'
                    break
                if (
                    (char == "r" or char == "R")
                    and ipc_client is not None
                    and not is_armed
                ):
                    # Reconcile control epoch before arming
                    try:
                        st = ipc_client.get_status(timeout_sec=0.5)
                        if st:
                            if st.get("current_epoch") is not None:
                                ipc_epoch = st["current_epoch"]
                            if (
                                st.get("active_owner") != owner_id
                                and st.get("active_owner") is None
                            ):
                                ok_acq, ep_acq, _, _ = ipc_client.acquire(
                                    owner_id,
                                    max_linear_speed=parsed.speed,
                                    max_angular_speed=parsed.angular_speed,
                                    timeout_sec=1.0,
                                )
                                if ok_acq and ep_acq is not None:
                                    ipc_epoch = ep_acq
                    except Exception:
                        pass

                    arm_ok, arm_err, arm_msg = ipc_client.arm(
                        epoch=ipc_epoch, tracks_raised=True, timeout_sec=2.0
                    )
                    if not arm_ok and arm_err == "INVALID_EPOCH":
                        try:
                            st = ipc_client.get_status(timeout_sec=0.5)
                            if st and st.get("current_epoch") is not None:
                                ipc_epoch = st["current_epoch"]
                                arm_ok, arm_err, arm_msg = ipc_client.arm(
                                    epoch=ipc_epoch,
                                    tracks_raised=True,
                                    timeout_sec=2.0,
                                )
                        except Exception:
                            pass

                    if arm_ok:
                        is_armed = True
                        current_key = None
                        seq = 0
                        print(
                            "\r\n[ARMED] Tracks-raised acknowledged. Driving enabled.\r\n"
                        )
                    else:
                        print(f"\r\n[ARM FAILED] {arm_msg} ({arm_err})\r\n")
                if char in key_to_dir:
                    current_key = char
                    last_key_time = now_mono
                if node is not None:
                    node.lease_mgr.process_key(char, now_mono)

            # Expire keyboard intent using the configured, bounded input lease
            if current_key and (now_mono - last_key_time > keyboard_lease):
                current_key = None

            if ipc_client is not None and ipc_epoch is not None:
                dir_cmd = (
                    key_to_dir.get(current_key, "neutral") if current_key else "neutral"
                )
                if dir_cmd == "stop":
                    try:
                        ipc_client.stop()
                    except Exception:
                        pass
                    is_armed = False
                    current_key = None
                    try:
                        st = ipc_client.get_status(timeout_sec=0.5)
                        if st and st.get("current_epoch") is not None:
                            ipc_epoch = st["current_epoch"]
                    except Exception:
                        pass
                elif is_armed:
                    c = None
                    try:
                        c = ipc_client.request_challenge(ipc_epoch, timeout_sec=0.2)
                    except Exception:
                        pass

                    if not c or "token" not in c:
                        is_armed = False
                        current_key = None
                        try:
                            st = ipc_client.get_status(timeout_sec=0.5)
                            if st and st.get("current_epoch") is not None:
                                ipc_epoch = st["current_epoch"]
                        except Exception:
                            pass
                    else:
                        seq += 1
                        try:
                            ok, cur_dir, err = ipc_client.submit_intent(
                                c["token"],
                                ipc_epoch,
                                seq,
                                dir_cmd,
                                timeout_sec=0.2,
                            )
                            if not ok:
                                is_armed = False
                                current_key = None
                                try:
                                    st = ipc_client.get_status(timeout_sec=0.5)
                                    if st and st.get("current_epoch") is not None:
                                        ipc_epoch = st["current_epoch"]
                                except Exception:
                                    pass
                        except Exception:
                            is_armed = False
                            current_key = None
                            try:
                                st = ipc_client.get_status(timeout_sec=0.5)
                                if st and st.get("current_epoch") is not None:
                                    ipc_epoch = st["current_epoch"]
                            except Exception:
                                pass
            elif node is not None:
                lin_x, ang_z = node.lease_mgr.get_velocities(time.monotonic())
                node.publish_twist(lin_x, ang_z)
                if rclpy is not None and rclpy.ok():
                    rclpy.spin_once(node, timeout_sec=0.0)

    except Exception as exc:
        sys.stderr.write(f"\nTeleop error: {exc}\n")
    finally:
        if ipc_client is not None:
            try:
                ipc_client.close()
            except Exception:
                pass
        if node is not None:
            node.publish_zero(count=3)
            if hasattr(node, "destroy_node") and rclpy is not None:
                node.destroy_node()
            if rclpy is not None and rclpy.ok():
                rclpy.shutdown()

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
        if direct_lock_fd is not None:
            from ubuntu_tank_operator.authority_lock import release_authority_lock

            release_authority_lock(direct_lock_fd)
            direct_lock_fd = None
        print("\nTeleop exited. Robot commanded to stop.")
    return 0


if __name__ == "__main__":
    main()
