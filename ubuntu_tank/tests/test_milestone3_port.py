"""
test_milestone3_port.py - Automated validation suite for Milestone 3 (ROS 2 Lyrical Port)

Verifies:
1. Complete elimination of legacy environment variables (MACHINE_TYPE) and paths (/home/ubuntu)
2. Hardware bridge parameterization, mock mode, and fail-closed serial errors
3. Bridge controller-only mode, monotonic freshness watchdog (250 ms), and signal-safe zeroing
4. Bridge and guard independent heartbeat emission for supervisor watchdog
5. Supervisor independent deadline tracking for guard and bridge
6. Controller odometry node parameterization (geometry, correction factors, controller_only topic routing)
7. Console script entrypoints integrity across all packages
8. Launch file argument declaration and node parameter mapping
9. build_workspace.sh CLI behavior, dry-run safety, and legacy env var rejection
"""

import ast
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# Configure module search paths
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
UBUNTU_TANK_DIR = os.path.dirname(TESTS_DIR)
WORKSPACE_ROOT = os.path.dirname(UBUNTU_TANK_DIR)
SRC_DIR = os.path.join(UBUNTU_TANK_DIR, "src")

# Ensure ubuntu_tank packages are importable
for pkg_name in [
    "ros_robot_controller",
    "controller",
    "ubuntu_tank_safety",
    "ubuntu_tank_supervisor",
    "ubuntu_tank_teleop",
]:
    pkg_path = os.path.join(SRC_DIR, pkg_name)
    if pkg_path not in sys.path:
        sys.path.insert(0, pkg_path)


# Setup lightweight mock ROS 2 infrastructure for hardware-free unit tests
class MockParameter:
    def __init__(self, value):
        self.value = value


class MockNode:
    def __init__(self, name="mock_node", **kwargs):
        self.name = name
        self._params = {}
        self.subscriptions = []
        self.publishers = []
        self.services = []
        self.timers = []
        self._logger = MagicMock()

    def declare_parameter(self, name, default_value):
        self._params[name] = default_value
        return MockParameter(default_value)

    def get_parameter(self, name):
        return MockParameter(self._params.get(name))

    def create_publisher(self, msg_type, topic, qos):
        pub = MagicMock(topic=topic, msg_type=msg_type)
        self.publishers.append(pub)
        return pub

    def create_subscription(self, msg_type, topic, callback, qos):
        sub = MagicMock(topic=topic, msg_type=msg_type, callback=callback)
        self.subscriptions.append(sub)
        return sub

    def create_service(self, srv_type, srv_name, callback):
        srv = MagicMock(srv_name=srv_name, srv_type=srv_type, callback=callback)
        self.services.append(srv)
        return srv

    def create_timer(self, period, callback):
        t = MagicMock(period=period, callback=callback)
        self.timers.append(t)
        return t

    def get_clock(self):
        clk = MagicMock()
        clk.now.return_value.to_msg.return_value = MagicMock()
        return clk

    def get_logger(self):
        return self._logger

    def destroy_node(self):
        pass


# Install mock modules for ROS dependencies
mock_rclpy = MagicMock()
mock_rclpy.ok.return_value = True
mock_rclpy_node = MagicMock()
mock_rclpy_node.Node = MockNode
mock_rclpy_qos = MagicMock()

sys.modules.setdefault("rclpy", mock_rclpy)
sys.modules.setdefault("rclpy.node", mock_rclpy_node)
sys.modules.setdefault("rclpy.qos", mock_rclpy_qos)
sys.modules.setdefault("std_srvs", MagicMock())
sys.modules.setdefault("std_srvs.srv", MagicMock())
sys.modules.setdefault("sensor_msgs", MagicMock())
sys.modules.setdefault("sensor_msgs.msg", MagicMock())
sys.modules.setdefault("std_msgs", MagicMock())
sys.modules.setdefault("std_msgs.msg", MagicMock())
sys.modules.setdefault("nav_msgs", MagicMock())
sys.modules.setdefault("nav_msgs.msg", MagicMock())
sys.modules.setdefault("geometry_msgs", MagicMock())
sys.modules.setdefault("geometry_msgs.msg", MagicMock())
sys.modules.setdefault("ros_robot_controller_msgs", MagicMock())
sys.modules.setdefault("ros_robot_controller_msgs.srv", MagicMock())
sys.modules.setdefault("ros_robot_controller_msgs.msg", MagicMock())


class TestLegacyEnvironmentAndPathRemoval(unittest.TestCase):
    """Exit criterion: clean code without legacy environment variables or paths."""

    def test_no_machine_type_in_sources(self):
        """Verify MACHINE_TYPE does not appear in any source files under ubuntu_tank/src."""
        violations = []
        for root, _, files in os.walk(SRC_DIR):
            for fn in files:
                if fn.endswith((".py", ".yaml", ".xml")):
                    fp = os.path.join(root, fn)
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    if "os.environ" in content and "MACHINE_TYPE" in content:
                        violations.append(
                            f"{os.path.relpath(fp, WORKSPACE_ROOT)} uses os.environ['MACHINE_TYPE']"
                        )
        self.assertEqual(
            violations,
            [],
            f"Found legacy MACHINE_TYPE environment access: {violations}",
        )

    def test_no_home_ubuntu_software_in_sources(self):
        """Verify hardcoded /home/ubuntu/software paths are completely removed from all source files."""
        violations = []
        for root, _, files in os.walk(SRC_DIR):
            for fn in files:
                if fn.endswith((".py", ".yaml", ".xml")):
                    fp = os.path.join(root, fn)
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    if "/home/ubuntu" in content:
                        violations.append(
                            f"{os.path.relpath(fp, WORKSPACE_ROOT)} contains /home/ubuntu"
                        )
        self.assertEqual(
            violations, [], f"Found hardcoded /home/ubuntu paths: {violations}"
        )


class TestRosRobotControllerSDK(unittest.TestCase):
    """Verify ros_robot_controller_sdk Board class enhancements."""

    def test_board_mock_initialization(self):
        """Board(device='mock') or Board(device=None) must initialize without serial hardware."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock", baudrate=1000000)
        self.assertTrue(board.is_mock)
        self.assertEqual(board.device, "mock")
        self.assertEqual(board.baudrate, 1000000)
        self.assertIsNone(board.port)

    def test_board_mock_buf_write_and_zero_motors(self):
        """Board in mock mode captures written packets including CRC8 checksums."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock")
        board.zero_motors(count=2)
        self.assertEqual(len(board.mock_written_buffers), 2)
        # Check start bytes (0xAA, 0x55)
        self.assertEqual(board.mock_written_buffers[0][0], 0xAA)
        self.assertEqual(board.mock_written_buffers[0][1], 0x55)

    def test_board_serial_open_failure_raises_runtime_error(self):
        """Board fails closed with clear RuntimeError when serial port cannot be opened."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        with self.assertRaises(RuntimeError) as ctx:
            Board(device="/nonexistent_dev_test_xyz123", baudrate=1000000)
        self.assertIn("Failed to open serial device", str(ctx.exception))
        self.assertIn("/nonexistent_dev_test_xyz123", str(ctx.exception))

    def test_board_close_signal_safe_idempotent(self):
        """Board.close() must be safe and idempotent."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock")
        board.close()
        board.close()  # Must not raise
        self.assertFalse(board.enable_recv)

    def test_board_write_timeout_configuration(self):
        """Board must configure a finite write_timeout on serial port."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock", write_timeout=0.100)
        self.assertEqual(board.write_timeout, 0.100)

    def test_board_rejects_nonpositive_write_timeout(self):
        """Board must reject timeouts that permit unbounded or invalid writes."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        for invalid_timeout in (0, -0.1, float("nan"), float("inf")):
            with self.subTest(write_timeout=invalid_timeout):
                with self.assertRaises(ValueError):
                    Board(device="mock", write_timeout=invalid_timeout)

    def test_board_rejects_invalid_read_and_silence_timeouts(self):
        """Read polling must be bounded below the fatal receive-silence limit."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        for read_timeout, silence_timeout in (
            (0, 0.5),
            (float("nan"), 0.5),
            (0.5, 0.5),
            (1.0, 0.5),
        ):
            with self.subTest(
                read_timeout=read_timeout, silence_timeout=silence_timeout
            ):
                with self.assertRaises(ValueError):
                    Board(
                        device="mock",
                        timeout=read_timeout,
                        silence_timeout=silence_timeout,
                    )

    def test_board_records_serial_read_failure(self):
        """The receive thread must expose serial read failure to the bridge without premature port closure."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock")
        board.is_mock = False
        board.enable_recv = True
        board.port = MagicMock(is_open=True)
        board.port.read.side_effect = OSError("USB disconnected")

        board.recv_task()

        self.assertIsInstance(board.fatal_error, RuntimeError)
        self.assertIn("Serial read error", str(board.fatal_error))
        # Port must remain open for shutdown stop writes until close() is called
        board.port.close.assert_not_called()
        board.close()
        board.port.close.assert_called_once()

    def test_board_records_persistent_serial_silence(self):
        """Repeated empty reads must become fatal after the silence deadline without premature port closure."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock", timeout=0.050, silence_timeout=0.500)
        board.is_mock = False
        board.enable_recv = True
        board._last_rx_time = time.monotonic() - 1.0
        board.port = MagicMock(is_open=True)
        board.port.read.return_value = b""

        board.recv_task()

        self.assertIsInstance(board.fatal_error, RuntimeError)
        self.assertIn("no bytes received", str(board.fatal_error))
        # Port must remain open for shutdown stop writes until close() is called
        board.port.close.assert_not_called()
        board.close()
        board.port.close.assert_called_once()

    def test_board_rx_failure_allows_motor_zeroing_before_closure(self):
        """On RX failure, stop packets must precede port closure while nonzero commands are blocked."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock", timeout=0.050, silence_timeout=0.500)
        board.is_mock = False
        board.enable_recv = True
        board._last_rx_time = time.monotonic() - 1.0
        fake_port = MagicMock(is_open=True)
        fake_port.read.return_value = b""
        fake_port.write.side_effect = lambda buf: len(buf)
        board.port = fake_port

        # Trigger RX silence timeout
        board.recv_task()
        self.assertIsNotNone(board.fatal_error)

        # Nonzero motor command must be rejected in fatal state
        with self.assertRaises(RuntimeError):
            board.set_motor_speed([[1, 0.5], [2, 0.5]])

        # Stop command (zeros) must succeed while port remains open
        board.zero_motors(count=4)
        self.assertEqual(fake_port.write.call_count, 4)
        fake_port.close.assert_not_called()

        # Orderly close completes shutdown
        board.close()
        fake_port.close.assert_called_once()

    def test_board_short_serial_write_raises_and_sets_fatal_error(self):
        """Short serial write must raise IOError and mark fatal error."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock")
        board.is_mock = False
        fake_port = MagicMock(is_open=True)
        fake_port.write.return_value = 1  # 1 byte instead of full packet
        board.port = fake_port

        with self.assertRaises(RuntimeError) as ctx:
            board.buf_write(1, [0x01, 0x02])
        self.assertIn("Short serial write", str(ctx.exception))
        self.assertIsNotNone(board.fatal_error)
        self.assertIn("Short serial write", str(board.fatal_error))

    def test_zero_motors_first_write_timeout_continues_remaining_attempts(self):
        """First write timeout must not cancel remaining stop attempts before port closure."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock")
        board.is_mock = False
        fake_port = MagicMock(is_open=True)

        call_idx = 0

        def fake_write(buf):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                raise TimeoutError("First write timed out")
            return len(buf)

        fake_port.write.side_effect = fake_write
        board.port = fake_port

        # Attempt 4 zero commands
        successful = board.zero_motors(count=4)
        # Attempt 1 failed, but attempts 2, 3, 4 succeeded
        self.assertEqual(successful, 3)
        self.assertEqual(fake_port.write.call_count, 4)
        fake_port.close.assert_not_called()

    def test_zero_motors_first_short_write_continues_remaining_attempts(self):
        """First short write must not cancel remaining stop attempts before port closure."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock")
        board.is_mock = False
        fake_port = MagicMock(is_open=True)

        call_idx = 0

        def fake_write(buf):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return 1  # Short write
            return len(buf)

        fake_port.write.side_effect = fake_write
        board.port = fake_port

        successful = board.zero_motors(count=4)
        self.assertEqual(successful, 3)
        self.assertEqual(fake_port.write.call_count, 4)
        fake_port.close.assert_not_called()

    def test_zero_motors_all_writes_fail_terminates_within_bound(self):
        """When all writes fail, zero_motors must terminate strictly within count bounds."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock")
        board.is_mock = False
        fake_port = MagicMock(is_open=True)
        fake_port.write.side_effect = OSError("Hardware disconnected")
        board.port = fake_port

        with self.assertRaises(RuntimeError):
            board.zero_motors(count=4)

        self.assertEqual(fake_port.write.call_count, 4)
        fake_port.close.assert_not_called()

    def test_board_get_battery_in_mock_mode(self):
        """Board in mock mode must report default or configured mock battery voltage."""
        from ros_robot_controller.ros_robot_controller_sdk import Board

        board = Board(device="mock")
        self.assertEqual(board.get_battery(), 12000)
        board.mock_battery_voltage = 11500
        self.assertEqual(board.get_battery(), 11500)

    def test_board_concurrent_motor_write_and_zero_motors_serialization(self):
        """Board _write_lock must serialize concurrent motor writes with zero_motors and close."""
        from ros_robot_controller.ros_robot_controller_sdk import Board
        import struct
        import threading
        import time

        board = Board(device="mock")
        board.is_mock = False
        fake_port = MagicMock(is_open=True)
        board.port = fake_port

        write_started = threading.Event()
        resume_write = threading.Event()
        written = []

        def fake_write(buf):
            if len(buf) >= 6 and buf[2] == 3 and buf[4] == 0x01:
                # Motor packet: inspect speeds
                is_zero = all(
                    spd == 0.0
                    for _, spd in [
                        struct.unpack("<Bf", buf[6 + i * 5 : 6 + i * 5 + 5])
                        for i in range(buf[5])
                    ]
                )
                if not is_zero:
                    write_started.set()
                    resume_write.wait(timeout=2.0)
                    written.append(("nonzero", buf))
                else:
                    written.append(("zero", buf))
            return len(buf)

        fake_port.write.side_effect = fake_write

        # Thread 1: start nonzero write
        t1 = threading.Thread(
            target=board.set_motor_speed, args=([[1, 1.0], [2, 1.0]],)
        )
        t1.start()

        self.assertTrue(write_started.wait(timeout=1.0))

        # Thread 2: call zero_motors while nonzero write is in-flight
        t2 = threading.Thread(target=board.zero_motors, kwargs={"count": 4})
        t2.start()

        time.sleep(0.05)
        self.assertTrue(
            t2.is_alive(),
            "zero_motors must wait for _write_lock held by in-flight write",
        )

        resume_write.set()
        t1.join(timeout=1.0)
        t2.join(timeout=1.0)

        # Verify wire order: nonzero must come before zeros
        types = [w[0] for w in written]
        self.assertEqual(types[0], "nonzero")
        self.assertEqual(types[1:], ["zero", "zero", "zero", "zero"])


class TestRosRobotControllerNode(unittest.TestCase):
    """Verify RosRobotController ROS 2 node porting, watchdog, and supervision."""

    def setUp(self):
        from ros_robot_controller.ros_robot_controller_sdk import Board

        self.orig_board = Board

    def tearDown(self):
        from ros_robot_controller import ros_robot_controller_node

        ros_robot_controller_node.Board = self.orig_board

    def test_parameter_declarations_and_controller_only_mode(self):
        """Verify parameters and controller-only mode endpoints."""
        from ros_robot_controller import ros_robot_controller_node

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge")

        # Verify declared parameters
        self.assertEqual(node.machine_type, "MentorPi_Tank")
        self.assertEqual(node.serial_device, "/dev/rrc")
        self.assertEqual(node.baud_rate, 1000000)
        self.assertTrue(node.controller_only)
        self.assertEqual(node.freshness_timeout_sec, 0.250)
        self.assertEqual(node.heartbeat_interval_sec, 0.200)
        self.assertEqual(node.serial_read_timeout_sec, 0.050)
        self.assertEqual(node.serial_silence_timeout_sec, 0.500)
        self.assertEqual(node.motor_topic, "/ros_robot_controller/set_motor_guarded")

        # Verify controller-only subscriptions and publishers
        sub_topics = [s.topic for s in node.subscriptions]
        pub_topics = [p.topic for p in node.publishers]
        service_names = [s.srv_name for s in node.services]

        self.assertIn("/ros_robot_controller/set_motor_guarded", sub_topics)
        self.assertIn("~/battery", pub_topics)

        # Forbidden command endpoints in controller-only mode
        forbidden_endpoints = [
            "~/set_led",
            "~/set_buzzer",
            "~/set_oled",
            "~/set_rgb",
            "~/bus_servo/set_state",
            "~/pwm_servo/set_state",
        ]
        for fe in forbidden_endpoints:
            self.assertNotIn(fe, sub_topics)
        self.assertNotIn("~/set_machine_type", service_names)

    def test_bridge_freshness_watchdog_zeroes_on_timeout(self):
        """Watchdog must send zero motors and enter fatal shutdown on freshness timeout."""
        from ros_robot_controller import ros_robot_controller_node
        import select

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge")
        node.running = True
        node.freshness_timeout_sec = 0.05  # 50 ms for test

        # Simulate receiving a command 100 ms ago
        node._last_motor_cmd_time = time.monotonic() - 0.10

        # Execute actual watchdog step implementation
        node._motor_watchdog_step()

        # Must enter fatal fault state
        self.assertTrue(node._fatal_fault)
        self.assertIsNone(node._last_motor_cmd_time)
        mock_board.zero_motors.assert_called_with(count=4)
        mock_board.close.assert_called()

        # Subsequent nonzero command must be dropped without advancing timestamp
        motor_msg = MagicMock()
        motor_msg.data = [MagicMock(id=1, rps=0.5)]
        node.set_motor_state(motor_msg)
        self.assertIsNone(node._last_motor_cmd_time)

        # Heartbeat emission must be suppressed
        r_fd, w_fd = os.pipe()
        try:
            node.bridge_pipe_fd = w_fd
            node._emit_heartbeat()
            r, _, _ = select.select([r_fd], [], [], 0.01)
            self.assertEqual(len(r), 0)
        finally:
            os.close(r_fd)
            os.close(w_fd)

    def test_watchdog_expiry_with_api_constrained_logger(self):
        """Fault handler must strictly use valid ROS logger methods and complete shutdown without raising AttributeError."""
        from ros_robot_controller import ros_robot_controller_node

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge")
        # Constrain logger to valid RcutilsLogger methods (specifically excluding 'critical')
        mock_logger = MagicMock(
            spec=["debug", "info", "warn", "warning", "error", "fatal"]
        )
        node.get_logger = MagicMock(return_value=mock_logger)

        node.running = True
        node.freshness_timeout_sec = 0.05
        node._last_motor_cmd_time = time.monotonic() - 0.10

        # Must execute cleanly without AttributeError
        node._motor_watchdog_step()

        self.assertTrue(node._fatal_fault)
        self.assertIsNone(node._last_motor_cmd_time)
        mock_logger.fatal.assert_called()
        mock_board.zero_motors.assert_called_with(count=4)
        mock_board.close.assert_called()

    def test_fatal_fault_resilient_to_logger_exceptions(self):
        """Even if logging fails or raises an exception, fatal shutdown and motor zeroing must execute."""
        from ros_robot_controller import ros_robot_controller_node

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge")
        mock_logger = MagicMock(
            spec=["debug", "info", "warn", "warning", "error", "fatal"]
        )
        mock_logger.fatal.side_effect = RuntimeError("Broken logger sink")
        node.get_logger = MagicMock(return_value=mock_logger)

        node._enter_fatal_fault("Fault with broken logger")

        self.assertTrue(node._fatal_fault)
        mock_board.zero_motors.assert_called_with(count=4)
        mock_board.close.assert_called()

    def test_safe_shutdown_zeroes_and_closes(self):
        """safe_shutdown() must send repeated zeros and close board."""
        from ros_robot_controller import ros_robot_controller_node

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge")
        node.safe_shutdown()

        mock_board.zero_motors.assert_called_with(count=4)
        mock_board.close.assert_called()
        self.assertTrue(node._shutting_down)

    def test_heartbeat_emission_via_pipe(self):
        """Bridge must emit monotonic timestamp to inherited pipe FD."""
        from ros_robot_controller import ros_robot_controller_node

        r_fd, w_fd = os.pipe()
        try:
            mock_board = MagicMock(is_mock=True)
            mock_board.fatal_error = None
            ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

            with patch.dict(os.environ, {"UBUNTU_TANK_BRIDGE_PIPE_FD": str(w_fd)}):
                node = ros_robot_controller_node.RosRobotController("test_bridge")
                self.assertEqual(node.bridge_pipe_fd, w_fd)

                before = time.monotonic()
                node._emit_heartbeat()
                after = time.monotonic()

                data = os.read(r_fd, 64).decode("ascii").strip()
                val = float(data)
                self.assertTrue(before <= val <= after + 0.1)
        finally:
            os.close(r_fd)
            os.close(w_fd)

    def test_serial_write_failure_marks_fatal_fault_and_suppresses_heartbeat(self):
        """Serial write failure must mark fatal fault, drop command, and suppress heartbeats."""
        from ros_robot_controller import ros_robot_controller_node
        import select

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        mock_board.set_motor_speed.side_effect = RuntimeError("USB write timeout")
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge")
        self.assertFalse(node._fatal_fault)
        self.assertIsNone(node._last_motor_cmd_time)

        motor_msg = MagicMock()
        item = MagicMock(id=1, rps=0.5)
        motor_msg.data = [item]

        node.set_motor_state(motor_msg)

        # Must enter fatal fault state
        self.assertTrue(node._fatal_fault)
        # Freshness timestamp MUST NOT advance on failed write!
        self.assertIsNone(node._last_motor_cmd_time)

        # Heartbeat emission must be completely suppressed
        r_fd, w_fd = os.pipe()
        try:
            node.bridge_pipe_fd = w_fd
            node._emit_heartbeat()
            r, _, _ = select.select([r_fd], [], [], 0.01)
            self.assertEqual(len(r), 0)
        finally:
            os.close(r_fd)
            os.close(w_fd)

    def test_controller_only_battery_telemetry_polling(self):
        """In controller-only mode, battery timer must poll and publish battery telemetry."""
        from ros_robot_controller import ros_robot_controller_node

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        mock_board.get_battery.return_value = 12450
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge")
        self.assertTrue(hasattr(node, "battery_timer"))

        # Invoke battery polling callback
        node._battery_timer_callback()

        mock_board.get_battery.assert_called()
        self.assertEqual(node.battery_pub.publish.call_count, 1)
        published_msg = node.battery_pub.publish.call_args[0][0]
        self.assertEqual(published_msg.data, 12450)

    def test_serial_read_failure_suppresses_heartbeat_and_stops_graph(self):
        """A receive-thread fault must be fatal before another heartbeat is sent."""
        from ros_robot_controller import ros_robot_controller_node
        import select

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = RuntimeError("Serial read error: USB disconnected")
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)
        node = ros_robot_controller_node.RosRobotController("test_bridge")

        r_fd, w_fd = os.pipe()
        try:
            node.bridge_pipe_fd = w_fd
            node._emit_heartbeat()
            readable, _, _ = select.select([r_fd], [], [], 0.01)
            self.assertEqual(readable, [])
        finally:
            os.close(r_fd)
            os.close(w_fd)

        self.assertTrue(node._fatal_fault)
        mock_board.close.assert_called()

    def test_rx_fault_during_inflight_command_serializes_stop_sequence(self):
        """Serialize RX-fault shutdown with in-flight motor writes; assert no nonzero frame follows stop zeros."""
        from ros_robot_controller import ros_robot_controller_node
        from ros_robot_controller.ros_robot_controller_sdk import Board
        import struct
        import threading
        import time

        # Construct actual RosRobotController with actual Board in mock mode
        ros_robot_controller_node.Board = lambda *args, **kwargs: Board(
            *args, **{**kwargs, "device": "mock"}
        )
        node = ros_robot_controller_node.RosRobotController("test_bridge")
        node.board.is_mock = False

        fake_port = MagicMock()
        fake_port.is_open = True
        node.board.port = fake_port

        command_in_flight = threading.Event()
        resume_write = threading.Event()
        written_frames = []
        frames_lock = threading.Lock()

        def parse_motor_frame(buf):
            # 0xAA, 0x55, func=3, len, subcmd=1, motor_count, [id, speed_float]...
            if (
                len(buf) >= 6
                and buf[0] == 0xAA
                and buf[1] == 0x55
                and buf[2] == 3
                and buf[4] == 0x01
            ):
                count = buf[5]
                speeds = []
                for i in range(count):
                    off = 6 + i * 5
                    if off + 5 <= len(buf) - 1:
                        mid, spd = struct.unpack("<Bf", buf[off : off + 5])
                        speeds.append((mid, spd))
                is_nonzero = any(spd != 0.0 for _, spd in speeds)
                return True, is_nonzero, speeds
            return False, False, []

        def tracked_write(buf):
            is_motor, is_nonzero, speeds = parse_motor_frame(buf)
            if is_motor:
                if is_nonzero:
                    # In-flight nonzero command write
                    command_in_flight.set()
                    if not resume_write.wait(timeout=2.0):
                        raise TimeoutError("Test timeout waiting for resume_write")
                    with frames_lock:
                        written_frames.append(("nonzero", speeds))
                else:
                    with frames_lock:
                        written_frames.append(("zero", speeds))
            else:
                with frames_lock:
                    written_frames.append(("other", bytes(buf)))
            return len(buf)

        def tracked_close():
            fake_port.is_open = False
            with frames_lock:
                written_frames.append(("close", None))

        fake_port.write.side_effect = tracked_write
        fake_port.close.side_effect = tracked_close

        # Clear initialization frames
        with frames_lock:
            written_frames.clear()

        # Thread 1: dispatch in-flight nonzero motor command
        motor_msg = MagicMock()
        motor_msg.data = [MagicMock(id=1, rps=1.2), MagicMock(id=2, rps=1.2)]

        cmd_thread = threading.Thread(target=node.set_motor_state, args=(motor_msg,))
        cmd_thread.start()

        # Wait until command write is in-flight (holding _motor_lock and _write_lock)
        self.assertTrue(
            command_in_flight.wait(timeout=1.0), "Command failed to enter write"
        )

        # Simulate background RX failure on the board
        node.board.fatal_error = RuntimeError("Serial read error: USB disconnected")

        # Thread 2: run watchdog step handling the RX fault
        wd_thread = threading.Thread(target=node._motor_watchdog_step)
        wd_thread.start()

        # Watchdog must block waiting for _motor_lock held by in-flight command
        time.sleep(0.05)
        self.assertTrue(
            wd_thread.is_alive(),
            "Watchdog should be waiting for _motor_lock held by in-flight command",
        )

        # Resume in-flight write to let it finish
        resume_write.set()
        cmd_thread.join(timeout=1.0)
        wd_thread.join(timeout=1.0)

        self.assertFalse(cmd_thread.is_alive())
        self.assertFalse(wd_thread.is_alive())

        # Inspect wire sequence
        with frames_lock:
            frame_types = [f[0] for f in written_frames]
            zero_indices = [
                idx for idx, (kind, _) in enumerate(written_frames) if kind == "zero"
            ]
            nonzero_indices = [
                idx for idx, (kind, _) in enumerate(written_frames) if kind == "nonzero"
            ]

        self.assertTrue(
            len(zero_indices) >= 4,
            f"Expected at least 4 zero frames, got {len(zero_indices)}",
        )
        self.assertIn("close", frame_types, "Port close was not recorded")

        first_zero_idx = zero_indices[0]
        for nz_idx in nonzero_indices:
            self.assertLess(
                nz_idx,
                first_zero_idx,
                f"Nonzero command at index {nz_idx} followed stop zero at index {first_zero_idx}: {frame_types}",
            )

        close_idx = frame_types.index("close")
        last_zero_idx = zero_indices[-1]
        self.assertGreater(
            close_idx,
            last_zero_idx,
            f"Port close at index {close_idx} preceded last zero stop at index {last_zero_idx}: {frame_types}",
        )

        # Node fatal fault state must be cleanly latched
        self.assertTrue(node._fatal_fault)
        self.assertTrue(node._shutting_down)
        self.assertIsNone(node._last_motor_cmd_time)

        # Subsequent motor commands must be completely dropped
        with frames_lock:
            before_count = len(written_frames)
        node.set_motor_state(motor_msg)
        with frames_lock:
            self.assertEqual(
                len(written_frames),
                before_count,
                "Subsequent command was not dropped in fatal state",
            )
        self.assertIsNone(node._last_motor_cmd_time)

    def test_command_arriving_during_shutdown_is_dropped_and_preserves_timestamp(self):
        """Motor commands arriving after or during shutdown must drop and not advance timestamp."""
        from ros_robot_controller import ros_robot_controller_node

        mock_board = MagicMock(is_mock=True)
        mock_board.fatal_error = None
        ros_robot_controller_node.Board = MagicMock(return_value=mock_board)

        node = ros_robot_controller_node.RosRobotController("test_bridge")
        node._fatal_fault = True
        node._last_motor_cmd_time = None

        motor_msg = MagicMock()
        motor_msg.data = [MagicMock(id=1, rps=1.0)]

        node.set_motor_state(motor_msg)

        mock_board.set_motor_speed.assert_not_called()
        self.assertIsNone(node._last_motor_cmd_time)


class TestOdomPublisherNode(unittest.TestCase):
    """Verify Controller (odom_publisher_node) parameterization and topic topology."""

    def test_host_velocity_caps_enforced_before_motor_publication(self):
        """Non-teleop commands cannot exceed lower host caps; invalid input stops."""
        from controller import odom_publisher_node
        from types import SimpleNamespace

        node = odom_publisher_node.Controller("test_controller")
        node.max_linear_speed = 0.05
        node.max_angular_speed = 0.1
        node.mecanum.set_velocity = MagicMock(return_value="bounded motors")
        for linear, angular, expected in [
            (0.2, 0.8, (0.05, 0.0, 0.1)),
            (-0.2, -0.8, (-0.05, 0.0, -0.1)),
            (float("nan"), 0.1, (0.0, 0.0, 0.0)),
        ]:
            message = SimpleNamespace(
                linear=SimpleNamespace(x=linear, y=0.0),
                angular=SimpleNamespace(z=angular),
            )
            node.cmd_vel_callback(message)
            node.mecanum.set_velocity.assert_called_with(*expected)
            node.motor_pub.publish.assert_called_with("bounded motors")

    def test_parameter_declarations_and_geometry(self):
        """Verify parameterized geometry and correction factors."""
        from controller import odom_publisher_node

        node = odom_publisher_node.Controller("test_controller")

        self.assertEqual(node.machine_type, "MentorPi_Tank")
        self.assertEqual(node.wheelbase, 0.1368)
        self.assertEqual(node.track_width, 0.1446)
        self.assertEqual(node.wheel_diameter, 0.075)
        self.assertTrue(node.controller_only)
        self.assertEqual(node.cmd_vel_topic, "/controller/cmd_vel")
        self.assertEqual(node.motor_output_topic, "/ubuntu_tank_safety/motor_input")
        self.assertEqual(node.left_correction, 1.0)
        self.assertEqual(node.right_correction, 1.0)

        # Verify chassis model received parameterized dimensions
        self.assertEqual(node.mecanum.wheelbase, 0.1368)
        self.assertEqual(node.mecanum.track_width, 0.1446)
        self.assertEqual(node.mecanum.wheel_diameter, 0.075)

    def test_controller_only_topic_routing(self):
        """In controller-only mode, only /controller/cmd_vel is subscribed and /ubuntu_tank_safety/motor_input published."""
        from controller import odom_publisher_node

        node = odom_publisher_node.Controller("test_controller")

        sub_topics = [s.topic for s in node.subscriptions]
        pub_topics = [p.topic for p in node.publishers]

        self.assertIn("/controller/cmd_vel", sub_topics)
        self.assertNotIn("/app/cmd_vel", sub_topics)
        self.assertNotIn("cmd_vel", sub_topics)
        self.assertNotIn("set_odom", sub_topics)

        self.assertIn("/ubuntu_tank_safety/motor_input", pub_topics)
        self.assertNotIn("ros_robot_controller/set_motor", pub_topics)
        self.assertNotIn("set_pose", pub_topics)

    def test_correction_factors_from_file(self):
        """Correction factors load cleanly from specified YAML file."""
        from controller import odom_publisher_node

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("left_correction_factor: 1.05\nright_correction_factor: 0.95\n")
            temp_yaml = f.name

        try:
            node = odom_publisher_node.Controller("test_controller")
            node.correction_file = temp_yaml
            factors = node.load_correction_factors()
            self.assertAlmostEqual(factors["left_correction_factor"], 1.05)
            self.assertAlmostEqual(factors["right_correction_factor"], 0.95)
        finally:
            if os.path.exists(temp_yaml):
                os.unlink(temp_yaml)

    def test_dead_reckoning_docstring(self):
        """Module docstring must explicitly state odom_raw is command integration, not measured odometry."""
        from controller import odom_publisher_node

        self.assertIsNotNone(odom_publisher_node.__doc__)
        self.assertIn("command integration", odom_publisher_node.__doc__.lower())
        self.assertIn("dead reckoning", odom_publisher_node.__doc__.lower())

    def test_angular_correction_applied_to_odometry(self):
        """Command-integrated yaw and twist must apply angular_correction_factor."""
        source_path = os.path.join(
            SRC_DIR, "controller", "controller", "odom_publisher_node.py"
        )
        with open(source_path, "r", encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertIn(
            "corrected_angular_z = self.angular_z * self.angular_factor", source
        )
        self.assertIn("delta_yaw = corrected_angular_z * self.dt", source)
        self.assertIn("self.odom.twist.twist.angular.z = corrected_angular_z", source)


class TestSupervisorIndependentDeadlines(unittest.TestCase):
    """Verify Supervisor tracks guard and bridge deadlines independently."""

    def test_independent_deadlines_evaluation(self):
        from ubuntu_tank_supervisor.supervisor import Supervisor

        sup = Supervisor(guard_deadline_sec=0.250, bridge_deadline_sec=0.250)

        # Initial state: neither received -> not healthy
        healthy, msg = sup.check_health(now_monotonic=10.0)
        self.assertFalse(healthy)
        self.assertIn("Guard heartbeat has never been received", msg)

        # Receive guard only
        sup.record_heartbeat("guard", timestamp_monotonic=10.0, is_trusted_channel=True)
        healthy, msg = sup.check_health(now_monotonic=10.05)
        self.assertFalse(healthy)
        self.assertIn("Bridge heartbeat has never been received", msg)

        # Receive bridge too -> healthy
        sup.record_heartbeat(
            "bridge", timestamp_monotonic=10.0, is_trusted_channel=True
        )
        healthy, msg = sup.check_health(now_monotonic=10.05)
        self.assertTrue(healthy)

        # Guard expires at 10.30, but bridge refreshed at 10.25 -> not healthy (guard expired)
        sup.record_heartbeat(
            "bridge", timestamp_monotonic=10.25, is_trusted_channel=True
        )
        healthy, msg = sup.check_health(now_monotonic=10.30)
        self.assertFalse(healthy)
        self.assertIn("Guard heartbeat is stale", msg)

        # Guard refreshed at 10.31, but bridge now expires at 10.55 -> not healthy (bridge expired)
        sup.record_heartbeat(
            "guard", timestamp_monotonic=10.31, is_trusted_channel=True
        )
        healthy, msg = sup.check_health(now_monotonic=10.55)
        self.assertFalse(healthy)
        self.assertIn("Bridge heartbeat is stale", msg)


class TestConsoleScriptsAndLaunch(unittest.TestCase):
    """Verify console scripts and launch files."""

    def test_console_script_entrypoints_exist(self):
        """Every console_scripts entrypoint must point to a callable function."""
        # 1. controller -> odom_publisher
        from controller.odom_publisher_node import main as odom_main

        self.assertTrue(callable(odom_main))

        # 2. ros_robot_controller -> ros_robot_controller
        from ros_robot_controller.ros_robot_controller_node import main as rrc_main

        self.assertTrue(callable(rrc_main))

        # 3. ubuntu_tank_safety -> motor_guard
        from ubuntu_tank_safety.motor_guard_node import main as guard_main

        self.assertTrue(callable(guard_main))

        # 4. ubuntu_tank_supervisor -> supervisor
        from ubuntu_tank_supervisor.supervisor_node import main as sup_main

        self.assertTrue(callable(sup_main))

        # 5. ubuntu_tank_teleop -> teleop_key
        from ubuntu_tank_teleop.teleop_key_node import main as teleop_main

        self.assertTrue(callable(teleop_main))

    def test_launch_file_declares_parameters(self):
        """Verify ros_robot_controller.launch.py declares required arguments."""
        launch_path = os.path.join(
            SRC_DIR, "ros_robot_controller", "launch", "ros_robot_controller.launch.py"
        )
        self.assertTrue(os.path.isfile(launch_path))
        with open(launch_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=launch_path)

        declared_args = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if getattr(node.func, "id", None) == "DeclareLaunchArgument":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        declared_args.add(node.args[0].value)

        expected = {
            "imu_frame",
            "serial_device",
            "baud_rate",
            "controller_only",
            "machine_type",
            "serial_read_timeout_sec",
            "write_timeout_sec",
            "serial_silence_timeout_sec",
        }
        self.assertTrue(
            expected.issubset(declared_args),
            f"Missing launch arguments: {expected - declared_args}",
        )


class TestBuildWorkspaceScript(unittest.TestCase):
    """Verify build_workspace.sh CLI behavior."""

    def test_build_workspace_help(self):
        cmd = [os.path.join(UBUNTU_TANK_DIR, "scripts", "build_workspace.sh"), "--help"]
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Usage:", res.stdout)
        self.assertIn("--dry-run", res.stdout)

    def test_build_workspace_dry_run(self):
        cmd = [
            os.path.join(UBUNTU_TANK_DIR, "scripts", "build_workspace.sh"),
            "--dry-run",
        ]
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertTrue(
            "Found 6 workspace packages" in res.stdout
            or "Found 7 workspace packages" in res.stdout
            or "Found 8 workspace packages" in res.stdout
        )
        self.assertIn("Dry-run complete", res.stdout)

    def test_build_workspace_rejects_legacy_machine_type_env(self):
        cmd = [
            os.path.join(UBUNTU_TANK_DIR, "scripts", "build_workspace.sh"),
            "--dry-run",
        ]
        env = dict(os.environ, MACHINE_TYPE="MentorPi_Tank")
        res = subprocess.run(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Legacy environment variable MACHINE_TYPE", res.stderr)

    def test_build_workspace_clean_validation_rejects_external_path(self):
        cmd = [
            os.path.join(UBUNTU_TANK_DIR, "scripts", "build_workspace.sh"),
            "--dry-run",
            "--clean",
            "--build-base",
            "/home/user/project",
        ]
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("outside workspace", res.stderr)

    def test_build_workspace_clean_validation_rejects_system_directory(self):
        cmd = [
            os.path.join(UBUNTU_TANK_DIR, "scripts", "build_workspace.sh"),
            "--dry-run",
            "--clean",
            "--build-base",
            "/",
        ]
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Refusing to clean broad or system directory", res.stderr)

    def test_build_workspace_clean_validation_rejects_protected_path(self):
        cmd = [
            os.path.join(UBUNTU_TANK_DIR, "scripts", "build_workspace.sh"),
            "--dry-run",
            "--clean",
            "--build-base",
            os.path.join(UBUNTU_TANK_DIR, "src"),
        ]
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Refusing to clean non-disposable workspace path", res.stderr)

    def test_build_workspace_clean_validation_rejects_workspace_root_file(self):
        """A custom clean base cannot target tracked files at workspace root."""
        cmd = [
            os.path.join(UBUNTU_TANK_DIR, "scripts", "build_workspace.sh"),
            "--dry-run",
            "--clean",
            "--build-base",
            os.path.join(UBUNTU_TANK_DIR, "README.md"),
        ]
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Refusing to clean non-disposable workspace path", res.stderr)


class TestPackageMetadataSynchronization(unittest.TestCase):
    """Verify package.xml and setup.py metadata synchronization."""

    def _assert_setup_metadata(self, content, **expected):
        """Compare literal setup metadata independently of Python quote style."""
        calls = [
            node
            for node in ast.walk(ast.parse(content))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setup"
        ]
        self.assertEqual(len(calls), 1)
        keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
        for name, value in expected.items():
            self.assertIn(name, keywords)
            self.assertEqual(ast.literal_eval(keywords[name]), value)

    def test_controller_metadata_sync(self):
        """controller package.xml and setup.py metadata must match."""
        import xml.etree.ElementTree as ET

        pkg_xml = os.path.join(SRC_DIR, "controller", "package.xml")
        tree = ET.parse(pkg_xml)
        root = tree.getroot()
        xml_ver = root.findtext("version")
        xml_lic = root.findtext("license")
        xml_maint = root.findtext("maintainer")

        setup_py = os.path.join(SRC_DIR, "controller", "setup.py")
        with open(setup_py, "r", encoding="utf-8") as f:
            content = f.read()

        self._assert_setup_metadata(
            content, version=xml_ver, license=xml_lic, maintainer=xml_maint
        )

    def test_ros_robot_controller_metadata_sync(self):
        """ros_robot_controller package.xml and setup.py metadata must match."""
        import xml.etree.ElementTree as ET

        pkg_xml = os.path.join(SRC_DIR, "ros_robot_controller", "package.xml")
        tree = ET.parse(pkg_xml)
        root = tree.getroot()
        xml_ver = root.findtext("version")
        xml_lic = root.findtext("license")
        xml_maint = root.findtext("maintainer")

        setup_py = os.path.join(SRC_DIR, "ros_robot_controller", "setup.py")
        with open(setup_py, "r", encoding="utf-8") as f:
            content = f.read()

        self._assert_setup_metadata(
            content, version=xml_ver, license=xml_lic, maintainer=xml_maint
        )


class TestInstallRos2ClosureManifest(unittest.TestCase):
    """Verify generate_candidate_manifest_from_archives logic."""

    def test_install_ros_rejects_live_candidate_override(self):
        """Live installation must inspect its own downloaded artifacts."""
        install_script = os.path.join(UBUNTU_TANK_DIR, "scripts", "install_ros2.sh")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as candidate_file:
            res = subprocess.run(
                [install_script, "install-ros", "--candidates", candidate_file.name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("test-only", res.stderr)

    def test_verify_closure_requires_architecture_and_repository(self):
        """Candidate entries cannot omit exact architecture or repository identity."""
        import yaml

        install_script = os.path.join(UBUNTU_TANK_DIR, "scripts", "install_ros2.sh")
        lock_path = os.path.join(UBUNTU_TANK_DIR, "versions.lock")
        with open(lock_path, "r", encoding="utf-8") as lock_file:
            packages = yaml.safe_load(lock_file)["packages"]

        lines = []
        for index, package in enumerate(packages):
            if index == 0:
                lines.append(
                    f"{package['name']} {package['version']} {package['sha256']}"
                )
            else:
                lines.append(
                    f"{package['name']} {package['version']} {package['architecture']} "
                    f"{package['repository']} {package['sha256']}"
                )

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as candidate_file:
            candidate_file.write("\n".join(lines) + "\n")
            candidate_file.flush()
            res = subprocess.run(
                [install_script, "verify-closure", "--candidates", candidate_file.name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("missing architecture", res.stderr)
        self.assertIn("missing repository", res.stderr)

    def test_generate_candidate_manifest_missing_locked_emitted(self):
        """Candidate manifest generation must record missing locked packages."""
        install_script = os.path.join(UBUNTU_TANK_DIR, "scripts", "install_ros2.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = os.path.join(tmpdir, "versions.lock")
            with open(lock_file, "w", encoding="utf-8") as f:
                f.write(
                    "packages:\n  - name: locked-pkg\n    version: 1.0.0\n    architecture: arm64\n    repository: ros2\n    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
                )

            archives_dir = os.path.join(tmpdir, "archives")
            os.makedirs(archives_dir, exist_ok=True)

            out_manifest = os.path.join(tmpdir, "candidates.txt")

            cmd = [
                "bash",
                "-c",
                f'source "{install_script}" && generate_candidate_manifest_from_archives "{lock_file}" "{out_manifest}" "{archives_dir}"',
            ]
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            self.assertEqual(res.returncode, 0)
            with open(out_manifest, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("locked-pkg MISSING-FROM-ARCHIVES", content)

    def test_generate_candidate_manifest_rejects_unreadable_artifact(self):
        """Every downloaded deb must be parseable rather than silently omitted."""
        install_script = os.path.join(UBUNTU_TANK_DIR, "scripts", "install_ros2.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = os.path.join(tmpdir, "versions.lock")
            archives_dir = os.path.join(tmpdir, "archives")
            fake_bin = os.path.join(tmpdir, "bin")
            out_manifest = os.path.join(tmpdir, "candidates.txt")
            os.makedirs(archives_dir)
            os.makedirs(fake_bin)
            with open(lock_file, "w", encoding="utf-8") as lock:
                lock.write("packages: []\n")
            with open(os.path.join(archives_dir, "broken.deb"), "wb") as artifact:
                artifact.write(b"not a deb")
            dpkg_deb = os.path.join(fake_bin, "dpkg-deb")
            with open(dpkg_deb, "w", encoding="utf-8") as executable:
                executable.write("#!/bin/sh\nexit 1\n")
            os.chmod(dpkg_deb, 0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            command = (
                f'source "{install_script}" && '
                f'generate_candidate_manifest_from_archives "{lock_file}" "{out_manifest}" "{archives_dir}"'
            )
            res = subprocess.run(
                ["bash", "-c", command], env=env, capture_output=True, text=True
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Cannot inspect downloaded artifact", res.stderr)

    def test_generate_candidate_manifest_rejects_duplicate_package(self):
        """Two artifacts for one package must fail instead of collapsing entries."""
        install_script = os.path.join(UBUNTU_TANK_DIR, "scripts", "install_ros2.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = os.path.join(tmpdir, "versions.lock")
            archives_dir = os.path.join(tmpdir, "archives")
            fake_bin = os.path.join(tmpdir, "bin")
            out_manifest = os.path.join(tmpdir, "candidates.txt")
            os.makedirs(archives_dir)
            os.makedirs(fake_bin)
            with open(lock_file, "w", encoding="utf-8") as lock:
                lock.write("packages: []\n")
            for filename in ("first.deb", "second.deb"):
                with open(os.path.join(archives_dir, filename), "wb") as artifact:
                    artifact.write(filename.encode("ascii"))
            dpkg_deb = os.path.join(fake_bin, "dpkg-deb")
            with open(dpkg_deb, "w", encoding="utf-8") as executable:
                executable.write("#!/bin/sh\nprintf 'duplicate-pkg\\n1.0\\narm64\\n'\n")
            os.chmod(dpkg_deb, 0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            command = (
                f'source "{install_script}" && '
                f'generate_candidate_manifest_from_archives "{lock_file}" "{out_manifest}" "{archives_dir}"'
            )
            res = subprocess.run(
                ["bash", "-c", command], env=env, capture_output=True, text=True
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Duplicate downloaded package 'duplicate-pkg'", res.stderr)

    def test_generate_candidate_manifest_includes_unlocked_archives(self):
        """Every downloaded deb, including unlocked transitives, must be emitted."""
        install_script = os.path.join(UBUNTU_TANK_DIR, "scripts", "install_ros2.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = os.path.join(tmpdir, "versions.lock")
            with open(lock_file, "w", encoding="utf-8") as f:
                f.write(
                    "packages:\n"
                    "  - name: locked-pkg\n"
                    "    version: 1.0.0\n"
                    "    architecture: arm64\n"
                    "    repository: ros2\n"
                    "    sha256: " + "0" * 64 + "\n"
                )

            archives_dir = os.path.join(tmpdir, "archives")
            fake_bin = os.path.join(tmpdir, "bin")
            os.makedirs(archives_dir)
            os.makedirs(fake_bin)
            for filename in ("locked-pkg.deb", "unlocked-transitive.deb"):
                with open(os.path.join(archives_dir, filename), "wb") as f:
                    f.write(filename.encode("ascii"))

            dpkg_deb = os.path.join(fake_bin, "dpkg-deb")
            with open(dpkg_deb, "w", encoding="utf-8") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    'case "$2" in\n'
                    "  *locked-pkg.deb) printf 'locked-pkg\\n1.0.0\\narm64\\n' ;;\n"
                    "  *) printf 'unlocked-transitive\\n2.0.0\\narm64\\n' ;;\n"
                    "esac\n"
                )
            os.chmod(dpkg_deb, 0o755)

            apt_cache = os.path.join(fake_bin, "apt-cache")
            with open(apt_cache, "w", encoding="utf-8") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    'if [ "$1" = madison ]; then\n'
                    '  case "$2" in\n'
                    "    locked-pkg) printf 'locked-pkg | 1.0.0 | https://packages.ros.org/ros2/ubuntu resolute/main arm64 Packages\\n' ;;\n"
                    "    *) printf 'unlocked-transitive | 2.0.0 | http://ports.ubuntu.com/ubuntu-ports resolute/main arm64 Packages\\n' ;;\n"
                    "  esac\n"
                    "fi\n"
                )
            os.chmod(apt_cache, 0o755)

            out_manifest = os.path.join(tmpdir, "candidates.txt")
            env = dict(os.environ)
            env["PATH"] = fake_bin + os.pathsep + env["PATH"]
            cmd = [
                "bash",
                "-c",
                f'source "{install_script}" && generate_candidate_manifest_from_archives "{lock_file}" "{out_manifest}" "{archives_dir}"',
            ]
            res = subprocess.run(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            with open(out_manifest, "r", encoding="utf-8") as f:
                candidates = f.read()
            self.assertIn("locked-pkg 1.0.0 arm64 ros2", candidates)
            self.assertIn("unlocked-transitive 2.0.0 arm64 ubuntu-resolute", candidates)

    def test_generate_candidate_manifest_from_real_deb(self):
        """Extraction must parse labeled fields from an authentic .deb artifact without labels leaking into output."""
        install_script = os.path.join(UBUNTU_TANK_DIR, "scripts", "install_ros2.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            deb_build_dir = os.path.join(tmpdir, "real_deb_pkg")
            debian_dir = os.path.join(deb_build_dir, "DEBIAN")
            os.makedirs(debian_dir)
            with open(os.path.join(debian_dir, "control"), "w", encoding="utf-8") as f:
                f.write(
                    "Package: authentic-tank-pkg\n"
                    "Version: 2.1.0\n"
                    "Architecture: arm64\n"
                    "Maintainer: Ubuntu Tank Maintainers <dev@mentorpi.local>\n"
                    "Description: Test deb for candidate extraction\n"
                )
            archives_dir = os.path.join(tmpdir, "archives")
            os.makedirs(archives_dir)
            real_deb_path = os.path.join(
                archives_dir, "authentic-tank-pkg_2.1.0_arm64.deb"
            )
            build_res = subprocess.run(
                ["dpkg-deb", "-b", deb_build_dir, real_deb_path],
                capture_output=True,
                text=True,
            )
            self.assertEqual(build_res.returncode, 0, build_res.stderr)

            lock_file = os.path.join(tmpdir, "versions.lock")
            with open(lock_file, "w", encoding="utf-8") as f:
                f.write("packages: []\n")

            out_manifest = os.path.join(tmpdir, "candidates.txt")
            cmd = f'source "{install_script}" && generate_candidate_manifest_from_archives "{lock_file}" "{out_manifest}" "{archives_dir}"'
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)

            with open(out_manifest, "r", encoding="utf-8") as f:
                content = f.read()

            # Must contain clean row without field labels
            self.assertIn("authentic-tank-pkg 2.1.0 arm64", content)
            self.assertNotIn("Package:", content)
            self.assertNotIn("Version:", content)
            self.assertNotIn("Architecture:", content)


if __name__ == "__main__":
    unittest.main()
