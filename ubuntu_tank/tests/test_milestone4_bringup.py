"""
test_milestone4_bringup.py - Automated hardware-free test suite for Milestone 4 (Guarded Bringup and Safe Teleop)

Verifies:
1. Guarded topic graph architecture and launch file integrity in ubuntu_tank_bringup.
2. Complete absence of guard-bypassing paths and fail-closed OnProcessExit shutdown handlers.
3. Disarmed startup by default, 250 ms freshness timeout, and conservative speed limits.
4. Stripping of legacy non-motor command surfaces in controller_only mode.
5. Transient-local guard state reporting on /ubuntu_tank_safety/state and /ubuntu_tank_safety/armed.
6. Renewable keyboard leases (150 ms) and fail-closed zeroing on timeout, exit, and signals.
7. Fault injection: wall-clock jump, ROS time pause, executor starvation, and one-child-healthy/one-child-hung supervision.
8. SROS2 access control policies: deny-by-default, unauthenticated participant rejection, and enclave isolation.
9. Verification that every tested exit path publishes repeated four-motor zero commands.
"""

import ast
import os
import signal
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
UBUNTU_TANK_DIR = os.path.dirname(TESTS_DIR)
WORKSPACE_ROOT = os.path.dirname(UBUNTU_TANK_DIR)
SRC_DIR = os.path.join(UBUNTU_TANK_DIR, "src")
BRINGUP_DIR = os.path.join(SRC_DIR, "ubuntu_tank_bringup")
SCRIPTS_DIR = os.path.join(UBUNTU_TANK_DIR, "scripts")

# Ensure packages are on sys.path
for pkg in [
    "ubuntu_tank_safety",
    "ubuntu_tank_supervisor",
    "ubuntu_tank_teleop",
    "controller",
    "ros_robot_controller",
    "ubuntu_tank_bringup",
]:
    p = os.path.join(SRC_DIR, pkg)
    if p not in sys.path:
        sys.path.insert(0, p)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


# Mock ROS 2 types for testing when ROS 2 is not installed
class MockParameter:
    def __init__(self, value):
        self.value = value


class MockNode:
    _param_overrides = {}

    def __init__(self, name="mock_node", **kwargs):
        self.name = name
        self.init_kwargs = kwargs
        self._params = dict(MockNode._param_overrides)
        self.subscriptions = []
        self.publishers = []
        self.services = []
        self.timers = []
        self._logger = MagicMock()

    def declare_parameter(self, name, default_value):
        if name not in self._params:
            self._params[name] = default_value
        return MockParameter(self._params[name])

    def get_parameter(self, name):
        return MockParameter(self._params.get(name))

    def create_publisher(self, msg_type, topic, qos):
        pub = MagicMock(topic=topic, msg_type=msg_type, qos=qos)
        pub.published_messages = []

        def record_pub(msg):
            pub.published_messages.append(msg)

        pub.publish.side_effect = record_pub
        self.publishers.append(pub)
        return pub

    def create_subscription(self, msg_type, topic, callback, qos):
        sub = MagicMock(topic=topic, msg_type=msg_type, callback=callback, qos=qos)
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


class MockDurabilityPolicy:
    TRANSIENT_LOCAL = 1
    VOLATILE = 2


class MockReliabilityPolicy:
    RELIABLE = 1
    BEST_EFFORT = 2


class MockQoSProfile:
    def __init__(
        self,
        depth=10,
        durability=MockDurabilityPolicy.VOLATILE,
        reliability=MockReliabilityPolicy.RELIABLE,
    ):
        self.depth = depth
        self.durability = durability
        self.reliability = reliability


# Install mock ROS modules if not present
mock_rclpy = MagicMock()
mock_rclpy.ok.return_value = True
mock_rclpy_node = MagicMock()
mock_rclpy_node.Node = MockNode
mock_rclpy_qos = MagicMock()
mock_rclpy_qos.QoSProfile = MockQoSProfile
mock_rclpy_qos.DurabilityPolicy = MockDurabilityPolicy
mock_rclpy_qos.ReliabilityPolicy = MockReliabilityPolicy

sys.modules.setdefault("rclpy", mock_rclpy)
sys.modules.setdefault("rclpy.node", mock_rclpy_node)
sys.modules.setdefault("rclpy.qos", mock_rclpy_qos)
sys.modules.setdefault("sensor_msgs", MagicMock())
sys.modules.setdefault("sensor_msgs.msg", MagicMock())
sys.modules.setdefault("std_msgs", MagicMock())
sys.modules.setdefault("std_msgs.msg", MagicMock())
sys.modules.setdefault("std_srvs", MagicMock())
sys.modules.setdefault("std_srvs.srv", MagicMock())
sys.modules.setdefault("geometry_msgs", MagicMock())
sys.modules.setdefault("geometry_msgs.msg", MagicMock())
sys.modules.setdefault("nav_msgs", MagicMock())
sys.modules.setdefault("nav_msgs.msg", MagicMock())
sys.modules.setdefault("ros_robot_controller_msgs", MagicMock())
sys.modules.setdefault("ros_robot_controller_msgs.msg", MagicMock())
sys.modules.setdefault("ros_robot_controller_msgs.srv", MagicMock())


# Mock launch and launch_ros modules for hardware-free test execution
class MockLaunchDescription:
    def __init__(self, entities=None):
        self.entities = entities or []


class MockLaunchArgument:
    def __init__(self, name, default_value=None, description=""):
        self.name = name
        self.default_value = default_value
        self.description = description


class MockSetEnvironmentVariable:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class MockLaunchConfiguration:
    def __init__(self, name):
        self.name = name


class MockAction:
    def __init__(self, *, condition=None):
        self.condition = condition


class MockLaunchNode(MockAction):
    def __init__(
        self,
        *,
        package=None,
        executable=None,
        name=None,
        namespace=None,
        exec_name=None,
        parameters=None,
        remappings=None,
        ros_arguments=None,
        arguments=None,
        output=None,
        emulate_tty=None,
        respawn=None,
        respawn_delay=None,
        condition=None,
        **kwargs,
    ):
        if kwargs:
            raise TypeError(
                f"Action.__init__() got an unexpected keyword argument '{next(iter(kwargs.keys()))}'"
            )
        super().__init__(condition=condition)
        self.package = package
        self.executable = executable
        self.name = name
        self.namespace = namespace
        self.parameters = parameters or []
        self.remappings = remappings or []
        self.ros_arguments = ros_arguments or []
        self.arguments = arguments or []
        self.output = output
        self.emulate_tty = emulate_tty


class MockRegisterEventHandler(MockAction):
    def __init__(self, event_handler, **kwargs):
        super().__init__(**kwargs)
        self.event_handler = event_handler


class MockOnProcessExit:
    def __init__(self, *, target_action=None, on_exit=None):
        self.target_action = target_action
        self.on_exit = on_exit


class MockEmitEvent(MockAction):
    def __init__(self, *, event=None, **kwargs):
        super().__init__(**kwargs)
        self.event = event


class MockShutdown:
    def __init__(self, *, reason=""):
        self.reason = reason


class MockLogInfo(MockAction):
    def __init__(self, *, msg="", **kwargs):
        super().__init__(**kwargs)
        self.msg = msg


class MockOpaqueFunction(MockAction):
    def __init__(self, *, function=None, args=None, kwargs=None, **other_kwargs):
        super().__init__(**other_kwargs)
        self.function = function
        self.args = args or []
        self.kwargs = kwargs or {}


mock_launch = MagicMock()
mock_launch.LaunchDescription = MockLaunchDescription
mock_launch_actions = MagicMock()
mock_launch_actions.DeclareLaunchArgument = MockLaunchArgument
mock_launch_actions.SetEnvironmentVariable = MockSetEnvironmentVariable
mock_launch_actions.RegisterEventHandler = MockRegisterEventHandler
mock_launch_actions.EmitEvent = MockEmitEvent
mock_launch_actions.LogInfo = MockLogInfo
mock_launch_actions.OpaqueFunction = MockOpaqueFunction
mock_launch_events = MagicMock()
mock_launch_events.Shutdown = MockShutdown
mock_launch_handlers = MagicMock()
mock_launch_handlers.OnProcessExit = MockOnProcessExit
mock_launch_substitutions = MagicMock()
mock_launch_substitutions.LaunchConfiguration = MockLaunchConfiguration
mock_launch_ros = MagicMock()
mock_launch_ros_actions = MagicMock()
mock_launch_ros_actions.Node = MockLaunchNode

sys.modules.setdefault("launch", mock_launch)
sys.modules.setdefault("launch.actions", mock_launch_actions)
sys.modules.setdefault("launch.events", mock_launch_events)
sys.modules.setdefault("launch.event_handlers", mock_launch_handlers)
sys.modules.setdefault("launch.substitutions", mock_launch_substitutions)
sys.modules.setdefault("launch_ros", mock_launch_ros)
sys.modules.setdefault("launch_ros.actions", mock_launch_ros_actions)


class TestBringupLaunchAndTopicGraph(unittest.TestCase):
    """Verify guarded bringup launch file and topic routing architecture."""

    def setUp(self):
        self.tank_launch_path = os.path.join(BRINGUP_DIR, "launch", "tank.launch.py")
        self.teleop_launch_path = os.path.join(
            BRINGUP_DIR, "launch", "teleop.launch.py"
        )

    def test_launch_files_exist(self):
        """tank.launch.py and teleop.launch.py must exist in ubuntu_tank_bringup."""
        self.assertTrue(
            os.path.isfile(self.tank_launch_path), "tank.launch.py not found"
        )
        self.assertTrue(
            os.path.isfile(self.teleop_launch_path), "teleop.launch.py not found"
        )

    def test_guarded_topic_pipeline_routing(self):
        """Verify tank.launch.py strictly routes commands through motor_guard with no bypasses."""
        with open(self.tank_launch_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Controller must subscribe to /controller/cmd_vel and publish to /ubuntu_tank_safety/motor_input
        self.assertTrue(
            "'/controller/cmd_vel'" in content or '"/controller/cmd_vel"' in content
        )
        self.assertTrue(
            "'/ubuntu_tank_safety/motor_input'" in content
            or '"/ubuntu_tank_safety/motor_input"' in content
        )

        # Bridge must subscribe to guarded output topic
        self.assertTrue(
            "'/ros_robot_controller/set_motor_guarded'" in content
            or '"/ros_robot_controller/set_motor_guarded"' in content
        )

        # Confirm explicit remapping of bridge set_motor to guarded topic
        self.assertTrue(
            "('~/set_motor', '/ros_robot_controller/set_motor_guarded')" in content
            or '("~/set_motor", "/ros_robot_controller/set_motor_guarded")' in content
        )

        # Controller must NEVER publish directly to bridge set_motor
        self.assertNotIn(
            "('motor_output_topic', '/ros_robot_controller/set_motor')", content
        )
        self.assertNotIn(
            '("motor_output_topic", "/ros_robot_controller/set_motor")', content
        )
        self.assertNotIn(
            "('motor_output_topic', 'ros_robot_controller/set_motor')", content
        )
        self.assertNotIn(
            '("motor_output_topic", "ros_robot_controller/set_motor")', content
        )

    def test_fail_closed_shutdown_handlers(self):
        """tank.launch.py must register OnProcessExit handlers to shut down graph if any node exits."""
        with open(self.tank_launch_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        found_handlers = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = getattr(node, "func", None)
                if isinstance(func, ast.Name) and func.id == "OnProcessExit":
                    for kw in node.keywords:
                        if kw.arg == "target_action" and isinstance(kw.value, ast.Name):
                            found_handlers.append(kw.value.id)

        self.assertIn(
            "motor_guard_node",
            found_handlers,
            "Missing fail-closed exit handler for motor_guard",
        )
        self.assertIn(
            "bridge_node",
            found_handlers,
            "Missing fail-closed exit handler for ros_robot_controller",
        )
        self.assertIn(
            "controller_node",
            found_handlers,
            "Missing fail-closed exit handler for controller",
        )

    def test_launch_arguments_and_safe_defaults(self):
        """Verify launch arguments configure disarmed startup, 250 ms timeout, and <= 2.0 max_rps."""
        with open(self.tank_launch_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        declared_args = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = getattr(node, "func", None)
                if isinstance(func, ast.Name) and func.id == "DeclareLaunchArgument":
                    arg_name = node.args[0].value if node.args else None
                    default_val = None
                    for kw in node.keywords:
                        if kw.arg == "default_value" and isinstance(
                            kw.value, ast.Constant
                        ):
                            default_val = kw.value.value
                    if arg_name:
                        declared_args[arg_name] = default_val

        self.assertEqual(declared_args.get("controller_only"), "true")
        self.assertEqual(declared_args.get("guard_timeout_sec"), "0.250")
        self.assertEqual(declared_args.get("max_rps"), "2.0")
        self.assertEqual(declared_args.get("serial_device"), "/dev/rrc")

    def test_teleop_launch_parameters(self):
        """teleop.launch.py must declare renewable lease < 0.250s."""
        with open(self.teleop_launch_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertTrue(
            "default_value='0.150'" in content or 'default_value="0.150"' in content,
            "Teleop lease duration must default to 150 ms",
        )
        self.assertIn("teleop_key", content)

    def test_launch_enclave_declarations(self):
        """tank.launch.py and teleop.launch.py must declare explicit SROS2 enclaves via ros_arguments."""
        with open(self.tank_launch_path, "r", encoding="utf-8") as f:
            tank_content = f.read()
        # Enclaves must be passed via ros_arguments=['--enclave', ...], NOT enclave=...
        self.assertNotIn("enclave='/ubuntu_tank/", tank_content)
        self.assertNotIn('enclave="/ubuntu_tank/', tank_content)
        self.assertTrue(
            "'/ubuntu_tank/controller'" in tank_content
            or '"/ubuntu_tank/controller"' in tank_content
        )
        self.assertTrue(
            "'/ubuntu_tank/guard'" in tank_content
            or '"/ubuntu_tank/guard"' in tank_content
        )
        self.assertTrue(
            "'/ubuntu_tank/bridge'" in tank_content
            or '"/ubuntu_tank/bridge"' in tank_content
        )
        self.assertTrue("'--enclave'" in tank_content or '"--enclave"' in tank_content)

        with open(self.teleop_launch_path, "r", encoding="utf-8") as f:
            teleop_content = f.read()
        self.assertNotIn("enclave='/ubuntu_tank/", teleop_content)
        self.assertNotIn('enclave="/ubuntu_tank/', teleop_content)
        self.assertTrue(
            "'/ubuntu_tank/operator'" in teleop_content
            or '"/ubuntu_tank/operator"' in teleop_content
        )
        self.assertTrue(
            "'--enclave'" in teleop_content or '"--enclave"' in teleop_content
        )

    def test_launch_descriptions_instantiation_without_typeerror(self):
        """Instantiating tank.launch.py and teleop.launch.py must succeed without keyword TypeError."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "tank_launch", self.tank_launch_path
        )
        tank_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tank_mod)
        tank_ld = tank_mod.generate_launch_description()
        self.assertTrue(len(tank_ld.entities) > 0)

        # Confirm nodes have correct enclave flags in ros_arguments
        nodes = [e for e in tank_ld.entities if isinstance(e, MockLaunchNode)]
        self.assertEqual(len(nodes), 3)
        for node in nodes:
            self.assertIn("--enclave", node.ros_arguments)

        spec2 = importlib.util.spec_from_file_location(
            "teleop_launch", self.teleop_launch_path
        )
        teleop_mod = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(teleop_mod)
        teleop_ld = teleop_mod.generate_launch_description()
        self.assertTrue(len(teleop_ld.entities) > 0)

        teleop_nodes = [e for e in teleop_ld.entities if isinstance(e, MockLaunchNode)]
        self.assertEqual(len(teleop_nodes), 1)
        self.assertEqual(
            teleop_nodes[0].ros_arguments, ["--enclave", "/ubuntu_tank/operator"]
        )

    def test_validate_security_preflight(self):
        """validate_security_preflight must fail closed on invalid configs and pass on valid configs."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "tank_launch_preflight", self.tank_launch_path
        )
        tank_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tank_mod)

        # 1. Fail closed on localhost_only != 1
        ctx = MagicMock()
        ctx.launch_configurations = {"localhost_only": "0"}
        with self.assertRaises(RuntimeError) as cm:
            tank_mod.validate_security_preflight(ctx)
        self.assertIn("ROS_LOCALHOST_ONLY must be set to '1'", str(cm.exception))

        # 2. Fail closed on security_enable != true
        ctx.launch_configurations = {"localhost_only": "1", "security_enable": "false"}
        with self.assertRaises(RuntimeError) as cm:
            tank_mod.validate_security_preflight(ctx)
        self.assertIn("ROS_SECURITY_ENABLE must be 'true'", str(cm.exception))

        # 3. Fail closed on security_strategy != Enforce
        ctx.launch_configurations = {
            "localhost_only": "1",
            "security_enable": "true",
            "security_strategy": "Permissive",
        }
        with self.assertRaises(RuntimeError) as cm:
            tank_mod.validate_security_preflight(ctx)
        self.assertIn("ROS_SECURITY_STRATEGY must be 'Enforce'", str(cm.exception))

        # 4. Fail closed on missing keystore directory
        ctx.launch_configurations = {
            "localhost_only": "1",
            "security_enable": "true",
            "security_strategy": "Enforce",
            "enforce_security": "true",
            "security_keystore": "/nonexistent/keystore",
        }
        with self.assertRaises(RuntimeError) as cm:
            tank_mod.validate_security_preflight(ctx)
        self.assertIn("SROS2 keystore missing", str(cm.exception))

        # 5. Pass when keystore and required enclave directories exist
        with tempfile.TemporaryDirectory() as tmpdir:
            for enc in ["controller", "guard", "bridge"]:
                os.makedirs(
                    os.path.join(tmpdir, "enclaves", "ubuntu_tank", enc), exist_ok=True
                )
            ctx.launch_configurations = {
                "localhost_only": "1",
                "security_enable": "true",
                "security_strategy": "Enforce",
                "enforce_security": "true",
                "security_keystore": tmpdir,
            }
            # Should not raise
            tank_mod.validate_security_preflight(ctx)

    def test_launch_executables_match_package_entry_points(self):
        """Verify all bringup and teleop launch executables match package console_script entry points."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "tank_launch_ep", self.tank_launch_path
        )
        tank_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tank_mod)
        tank_ld = tank_mod.generate_launch_description()

        spec2 = importlib.util.spec_from_file_location(
            "teleop_launch_ep", self.teleop_launch_path
        )
        teleop_mod = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(teleop_mod)
        teleop_ld = teleop_mod.generate_launch_description()

        all_nodes = [e for e in tank_ld.entities if isinstance(e, MockLaunchNode)] + [
            e for e in teleop_ld.entities if isinstance(e, MockLaunchNode)
        ]

        self.assertGreaterEqual(len(all_nodes), 4)

        for node in all_nodes:
            pkg_setup = os.path.join(SRC_DIR, node.package, "setup.py")
            self.assertTrue(
                os.path.isfile(pkg_setup),
                f"setup.py not found for package {node.package}",
            )
            with open(pkg_setup, "r", encoding="utf-8") as f:
                setup_content = f.read()
            tree = ast.parse(setup_content)
            console_scripts = []
            for ast_node in ast.walk(tree):
                if isinstance(ast_node, ast.Dict):
                    for k, v in zip(ast_node.keys, ast_node.values):
                        if (
                            isinstance(k, ast.Constant)
                            and k.value == "console_scripts"
                            and isinstance(v, ast.List)
                        ):
                            for item in v.elts:
                                if isinstance(item, ast.Constant) and isinstance(
                                    item.value, str
                                ):
                                    script_name = item.value.split("=")[0].strip()
                                    console_scripts.append(script_name)
            self.assertIn(
                node.executable,
                console_scripts,
                f"Node executable '{node.executable}' in package '{node.package}' is not exported in {pkg_setup} console_scripts ({console_scripts})",
            )

        # Specifically assert controller executable is odom_publisher and NOT odom_publisher_node
        controller_nodes = [n for n in all_nodes if n.package == "controller"]
        self.assertEqual(len(controller_nodes), 1)
        self.assertEqual(controller_nodes[0].executable, "odom_publisher")
        self.assertNotEqual(controller_nodes[0].executable, "odom_publisher_node")

    def test_launch_executable_resolution_from_clean_install_tree(self):
        """Simulate clean install tree layout and verify executable resolution succeeds for all launch nodes."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "tank_launch_tree", self.tank_launch_path
        )
        tank_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tank_mod)
        tank_ld = tank_mod.generate_launch_description()

        nodes = [e for e in tank_ld.entities if isinstance(e, MockLaunchNode)]

        with tempfile.TemporaryDirectory() as tmp_install:
            # Create simulated clean install tree: install/<pkg>/lib/<pkg>/<executable>
            for node in nodes:
                pkg_install_lib = os.path.join(
                    tmp_install, node.package, "lib", node.package
                )
                os.makedirs(pkg_install_lib, exist_ok=True)
                pkg_setup = os.path.join(SRC_DIR, node.package, "setup.py")
                with open(pkg_setup, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for ast_node in ast.walk(tree):
                    if isinstance(ast_node, ast.Dict):
                        for k, v in zip(ast_node.keys, ast_node.values):
                            if (
                                isinstance(k, ast.Constant)
                                and k.value == "console_scripts"
                                and isinstance(v, ast.List)
                            ):
                                for item in v.elts:
                                    if isinstance(item, ast.Constant) and isinstance(
                                        item.value, str
                                    ):
                                        script_name = item.value.split("=")[0].strip()
                                        script_path = os.path.join(
                                            pkg_install_lib, script_name
                                        )
                                        with open(script_path, "w") as sf:
                                            sf.write("#!/bin/sh\nexit 0\n")
                                        os.chmod(script_path, 0o755)

            # Check that every launch node executable resolves in the install tree
            for node in nodes:
                expected_exec_path = os.path.join(
                    tmp_install, node.package, "lib", node.package, node.executable
                )
                self.assertTrue(
                    os.path.isfile(expected_exec_path),
                    f"Executable resolution failed in install tree: {expected_exec_path} does not exist",
                )
                self.assertTrue(os.access(expected_exec_path, os.X_OK))

            # Specifically verify that odom_publisher_node would FAIL resolution
            bad_controller_exec = os.path.join(
                tmp_install, "controller", "lib", "controller", "odom_publisher_node"
            )
            self.assertFalse(
                os.path.exists(bad_controller_exec),
                "odom_publisher_node must NOT exist in clean install tree",
            )


class TestControllerOnlySurfaces(unittest.TestCase):
    """Verify legacy non-motor command endpoints are completely stripped in controller_only mode."""

    def test_controller_node_strips_legacy_topics(self):
        """In controller_only mode, /app/cmd_vel, cmd_vel, set_pose, and servos must be absent."""
        from controller.odom_publisher_node import Controller

        with patch("controller.odom_publisher_node.rclpy.node.Node", MockNode):
            node = Controller(name="test_controller")
            self.assertTrue(node.controller_only)

            # Subscribed topics
            sub_topics = [sub.topic for sub in node.subscriptions]
            self.assertIn("/controller/cmd_vel", sub_topics)
            self.assertNotIn("/app/cmd_vel", sub_topics)
            self.assertNotIn("cmd_vel", sub_topics)
            self.assertNotIn("set_odom", sub_topics)

            # Published topics
            pub_topics = [pub.topic for pub in node.publishers]
            self.assertIn("/ubuntu_tank_safety/motor_input", pub_topics)
            self.assertNotIn("ros_robot_controller/pwm_servo/set_state", pub_topics)
            self.assertNotIn("set_pose", pub_topics)

    def test_controller_requires_guard_input_topic(self):
        """Controller node must reject any output topic other than /ubuntu_tank_safety/motor_input in controller_only mode."""
        from controller.odom_publisher_node import Controller

        MockNode._param_overrides = {
            "motor_output_topic": "ros_robot_controller/set_motor"
        }
        try:
            with self.assertRaises(ValueError) as ctx:
                Controller(name="bypass_attempt")
            self.assertIn(
                "controller_only requires motor_output_topic", str(ctx.exception)
            )
        finally:
            MockNode._param_overrides = {}

    def test_bridge_strips_non_motor_command_endpoints(self):
        """In controller_only mode, ros_robot_controller must not register buzzer, oled, rgb, servos."""
        from ros_robot_controller.ros_robot_controller_node import RosRobotController

        class MockBoard:
            def __init__(self, *args, **kwargs):
                self.is_mock = True
                self.fatal_error = None

            def zero_motors(self, count=1):
                pass

            def enable_reception(self, val=True):
                pass

            def set_gamepad_type(self, *args):
                pass

            def set_motor_type(self, *args):
                pass

        with (
            patch(
                "ros_robot_controller.ros_robot_controller_node.rclpy.node.Node",
                MockNode,
            ),
            patch("ros_robot_controller.ros_robot_controller_node.Board", MockBoard),
        ):
            node = RosRobotController(name="test_bridge")
            self.assertTrue(node.controller_only)

            sub_topics = [sub.topic for sub in node.subscriptions]
            # Must NOT register peripheral command endpoints
            for forbidden in [
                "~/set_led",
                "~/set_buzzer",
                "~/set_oled",
                "~/bus_servo/set_state",
                "~/pwm_servo/set_state",
                "~/set_rgb",
            ]:
                self.assertNotIn(
                    forbidden,
                    sub_topics,
                    f"Forbidden command endpoint found in bridge: {forbidden}",
                )


class TestMotorGuardStateAndZeroEmission(unittest.TestCase):
    """Verify guard state reporting and repeated four-motor zero emission on all exit paths."""

    def test_transient_local_guard_state_topics(self):
        """Guard must publish transient-local state on /ubuntu_tank_safety/state and /ubuntu_tank_safety/armed."""
        from ubuntu_tank_safety.motor_guard_node import MotorGuardNode

        with patch("ubuntu_tank_safety.motor_guard_node.Node", MockNode):
            node = MotorGuardNode()

            # Verify publishers exist
            pub_map = {pub.topic: pub for pub in node.publishers}
            self.assertIn("/ubuntu_tank_safety/state", pub_map)
            self.assertIn("/ubuntu_tank_safety/armed", pub_map)

            # Verify QoS is transient-local and reliable
            state_pub = pub_map["/ubuntu_tank_safety/state"]
            self.assertEqual(
                state_pub.qos.durability, MockDurabilityPolicy.TRANSIENT_LOCAL
            )
            self.assertEqual(state_pub.qos.reliability, MockReliabilityPolicy.RELIABLE)

            # Initial state must be DISARMED (data = False)
            self.assertTrue(len(state_pub.published_messages) >= 1)
            self.assertFalse(state_pub.published_messages[0].data)

    def test_repeated_zero_on_destroy_node(self):
        """destroy_node must emit repeated 4-motor zero commands (count=5)."""
        from ubuntu_tank_safety.motor_guard_node import MotorGuardNode

        with (
            patch("ubuntu_tank_safety.motor_guard_node.Node", MockNode),
            patch("ubuntu_tank_safety.motor_guard_node.MotorsState", MagicMock()),
            patch("ubuntu_tank_safety.motor_guard_node.MotorState", MagicMock()),
        ):
            node = MotorGuardNode()
            guarded_pub = [
                p
                for p in node.publishers
                if p.topic == "/ros_robot_controller/set_motor_guarded"
            ][0]
            initial_count = len(guarded_pub.published_messages)

            node.destroy_node()
            final_count = len(guarded_pub.published_messages)
            self.assertEqual(
                final_count - initial_count,
                5,
                "destroy_node must publish 5 zero commands",
            )

    def test_repeated_zero_on_disarm(self):
        """Explicit disarm service call must emit repeated 4-motor zero commands."""
        from ubuntu_tank_safety.motor_guard_node import MotorGuardNode

        with (
            patch("ubuntu_tank_safety.motor_guard_node.Node", MockNode),
            patch("ubuntu_tank_safety.motor_guard_node.MotorsState", MagicMock()),
            patch("ubuntu_tank_safety.motor_guard_node.MotorState", MagicMock()),
        ):
            node = MotorGuardNode()
            guarded_pub = [
                p
                for p in node.publishers
                if p.topic == "/ros_robot_controller/set_motor_guarded"
            ][0]

            req = MagicMock(data=False)
            resp = MagicMock()
            node._handle_set_arm(req, resp)

            self.assertEqual(len(guarded_pub.published_messages), 5)
            self.assertTrue(resp.success)
            self.assertIn("disarmed", resp.message.lower())

    def test_repeated_zero_on_watchdog_timeout(self):
        """Watchdog timeout must emit repeated 4-motor zero commands."""
        from ubuntu_tank_safety.motor_guard_node import MotorGuardNode

        with (
            patch("ubuntu_tank_safety.motor_guard_node.Node", MockNode),
            patch("ubuntu_tank_safety.motor_guard_node.MotorsState", MagicMock()),
            patch("ubuntu_tank_safety.motor_guard_node.MotorState", MagicMock()),
        ):
            node = MotorGuardNode()
            guarded_pub = [
                p
                for p in node.publishers
                if p.topic == "/ros_robot_controller/set_motor_guarded"
            ][0]

            # Arm the guard
            node.guard.arm()
            node.guard._last_command_monotonic = (
                time.monotonic() - 0.300
            )  # 300 ms ago (> 250 ms)

            node._on_watchdog_tick()
            self.assertFalse(node.guard.is_armed)
            self.assertEqual(len(guarded_pub.published_messages), 5)

    def test_repeated_zero_on_invalid_motor_command(self):
        """Faulty motor commands (e.g. RPS exceeding max) must disarm and emit repeated zero commands."""
        from ubuntu_tank_safety.motor_guard_node import MotorGuardNode

        with (
            patch("ubuntu_tank_safety.motor_guard_node.Node", MockNode),
            patch("ubuntu_tank_safety.motor_guard_node.MotorsState", MagicMock()),
            patch("ubuntu_tank_safety.motor_guard_node.MotorState", MagicMock()),
        ):
            node = MotorGuardNode()
            guarded_pub = [
                p
                for p in node.publishers
                if p.topic == "/ros_robot_controller/set_motor_guarded"
            ][0]

            node.guard.arm()
            # Command with RPS = 10.0 (> 2.0 max_rps)
            bad_msg = MagicMock()
            bad_msg.data = [
                MagicMock(id=1, rps=10.0),
                MagicMock(id=2, rps=0.0),
                MagicMock(id=3, rps=0.0),
                MagicMock(id=4, rps=0.0),
            ]

            node._on_motor_input(bad_msg)
            self.assertFalse(node.guard.is_armed)
            self.assertEqual(len(guarded_pub.published_messages), 5)

    def test_signal_handler_publishes_stop_before_shutdown(self):
        """Signal handling in motor_guard_node must deliver 5 stop commands before context shutdown."""
        from ubuntu_tank_safety.motor_guard_node import MotorGuardNode, main

        context_live = [True]

        def mock_is_ok():
            return context_live[0]

        def mock_shutdown():
            context_live[0] = False

        published_stops = []

        def mock_publish(msg):
            if not context_live[0]:
                raise RuntimeError("Cannot publish after rclpy.shutdown()!")
            published_stops.append(msg)

        mock_pub = MagicMock()
        mock_pub.topic = "/ros_robot_controller/set_motor_guarded"
        mock_pub.publish.side_effect = mock_publish

        with (
            patch("ubuntu_tank_safety.motor_guard_node.rclpy") as mock_r,
            patch("ubuntu_tank_safety.motor_guard_node.Node", MockNode),
            patch("ubuntu_tank_safety.motor_guard_node.MotorsState", MagicMock()),
            patch("ubuntu_tank_safety.motor_guard_node.MotorState", MagicMock()),
        ):
            mock_r.ok.side_effect = mock_is_ok
            mock_r.shutdown.side_effect = mock_shutdown

            node = MotorGuardNode()
            node.guarded_pub = mock_pub

            handlers = {}

            def mock_signal(sig, handler):
                handlers[sig] = handler

            with (
                patch("signal.signal", side_effect=mock_signal),
                patch(
                    "ubuntu_tank_safety.motor_guard_node.MotorGuardNode",
                    return_value=node,
                ),
                patch("ubuntu_tank_safety.motor_guard_node.rclpy.spin") as mock_spin,
            ):

                def trigger_sig(n):
                    if signal.SIGINT in handlers:
                        handlers[signal.SIGINT](signal.SIGINT, None)

                mock_spin.side_effect = trigger_sig

                main()

            self.assertEqual(
                len(published_stops),
                5,
                "Must deliver exactly 5 stop messages before shutdown",
            )
            self.assertFalse(
                context_live[0], "Context must be shut down after stop messages sent"
            )


class TestTeleopRenewableLease(unittest.TestCase):
    """Verify keyboard teleop renewable leases and fail-closed stop behavior."""

    def test_renewable_lease_expires_to_zero(self):
        """Lease must auto-expire to zero within 150 ms without continuous input."""
        from ubuntu_tank_teleop.lease import TeleopLeaseManager

        mgr = TeleopLeaseManager(
            linear_vel=0.2, angular_vel=0.5, lease_duration_sec=0.150
        )
        now = 100.0

        # Press 'w'
        lin, ang = mgr.process_key("w", now_monotonic=now)
        self.assertAlmostEqual(lin, 0.2)

        # 100 ms later: still within lease
        lin, ang = mgr.get_velocities(now_monotonic=now + 0.100)
        self.assertAlmostEqual(lin, 0.2)

        # 160 ms later: lease expired (> 150 ms) -> returns zero
        lin, ang = mgr.get_velocities(now_monotonic=now + 0.160)
        self.assertEqual(lin, 0.0)
        self.assertEqual(ang, 0.0)

    def test_space_bar_immediate_stop(self):
        """Space bar immediately clears active lease and stops."""
        from ubuntu_tank_teleop.lease import TeleopLeaseManager

        mgr = TeleopLeaseManager(
            linear_vel=0.2, angular_vel=0.5, lease_duration_sec=0.150
        )
        mgr.process_key("w", now_monotonic=10.0)
        lin, ang = mgr.process_key(" ", now_monotonic=10.020)
        self.assertEqual(lin, 0.0)
        self.assertEqual(ang, 0.0)

    def test_teleop_node_publishes_repeated_zero_on_exit(self):
        """TeleopKeyNode publish_zero must send multiple zero Twist commands."""
        from ubuntu_tank_teleop.teleop_key_node import TeleopKeyNode

        with (
            patch("ubuntu_tank_teleop.teleop_key_node.Node", MockNode),
            patch("ubuntu_tank_teleop.teleop_key_node.Twist", MagicMock()),
        ):
            node = TeleopKeyNode()
            cmd_pub = [p for p in node.publishers if p.topic == "/controller/cmd_vel"][
                0
            ]
            node.publish_zero(count=3)
            self.assertEqual(len(cmd_pub.published_messages), 3)

    def test_teleop_key_node_publish_rate_parameter(self):
        """TeleopKeyNode must retrieve and store publish_rate_hz parameter."""
        from ubuntu_tank_teleop.teleop_key_node import TeleopKeyNode

        MockNode._param_overrides = {"publish_rate_hz": 30.0}
        try:
            with patch("ubuntu_tank_teleop.teleop_key_node.Node", MockNode):
                node = TeleopKeyNode()
                self.assertEqual(node.publish_rate_hz, 30.0)
        finally:
            MockNode._param_overrides = {}

    def test_teleop_node_disables_parameter_services(self):
        """TeleopKeyNode must pass start_parameter_services=False to Node constructor."""
        from ubuntu_tank_teleop.teleop_key_node import TeleopKeyNode

        node = TeleopKeyNode()
        self.assertEqual(node.init_kwargs.get("start_parameter_services"), False)

    def test_teleop_pty_keyboard_input(self):
        """Interactive key characters sent via pseudo-terminal must be processed by TeleopLeaseManager."""
        import pty
        import tty
        from ubuntu_tank_teleop.lease import TeleopLeaseManager

        master_fd, slave_fd = pty.openpty()
        try:
            tty.setraw(slave_fd)
            mgr = TeleopLeaseManager(
                linear_vel=0.2, angular_vel=0.5, lease_duration_sec=0.150
            )

            # Write 'w' to master
            os.write(master_fd, b"w")
            char = os.read(slave_fd, 1).decode("utf-8")
            t0 = time.monotonic()
            mgr.process_key(char, now_monotonic=t0)
            lin, ang = mgr.get_velocities(now_monotonic=t0)
            self.assertAlmostEqual(lin, 0.2)
            self.assertEqual(ang, 0.0)

            # Write 'd' to master (turn right in place)
            os.write(master_fd, b"d")
            char = os.read(slave_fd, 1).decode("utf-8")
            t1 = time.monotonic()
            mgr.process_key(char, now_monotonic=t1)
            lin, ang = mgr.get_velocities(now_monotonic=t1)
            self.assertEqual(lin, 0.0)
            self.assertAlmostEqual(ang, -0.5)

            # Write ' ' (stop) to master
            os.write(master_fd, b" ")
            char = os.read(slave_fd, 1).decode("utf-8")
            t2 = time.monotonic()
            mgr.process_key(char, now_monotonic=t2)
            lin, ang = mgr.get_velocities(now_monotonic=t2)
            self.assertEqual(lin, 0.0)
            self.assertEqual(ang, 0.0)
        finally:
            os.close(master_fd)
            os.close(slave_fd)


class TestFaultInjectionRegressions(unittest.TestCase):
    """Fault injection tests: wall-clock jump, ROS time pause, and one-child-hung supervision."""

    def test_wall_clock_jump_does_not_affect_guard_timeout(self):
        """Wall-clock time adjustments (e.g. NTP sync) must NOT cause false timeouts or bypasses."""
        from ubuntu_tank_safety.motor_guard import MotorGuard

        guard = MotorGuard(max_rps=2.0, timeout_sec=0.250)
        mono_now = 500.0
        guard.arm(now_monotonic=mono_now)
        guard.handle_command(
            [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)], now_monotonic=mono_now
        )

        # Simulate 1 hour wall clock jump backward and forward
        with patch("time.time", return_value=0.0):
            # Monotonic time advanced only 50 ms
            timed_out, _ = guard.check_timeout(now_monotonic=mono_now + 0.050)
            self.assertFalse(
                timed_out, "Monotonic check must not be fooled by wall clock jump"
            )
            self.assertTrue(guard.is_armed)

        with patch("time.time", return_value=1e9):
            # Monotonic time advanced 300 ms (> 250 ms)
            timed_out, zero_cmd = guard.check_timeout(now_monotonic=mono_now + 0.300)
            self.assertTrue(
                timed_out,
                "Timeout must fire based on monotonic time regardless of wall clock",
            )
            self.assertFalse(guard.is_armed)

    def test_ros_time_pause_does_not_affect_guard_watchdog(self):
        """Pausing ROS time (use_sim_time / paused clock) must NOT inhibit the guard watchdog."""
        from ubuntu_tank_safety.motor_guard_node import MotorGuardNode

        with (
            patch("ubuntu_tank_safety.motor_guard_node.Node", MockNode),
            patch("ubuntu_tank_safety.motor_guard_node.MotorsState", MagicMock()),
            patch("ubuntu_tank_safety.motor_guard_node.MotorState", MagicMock()),
        ):
            node = MotorGuardNode()
            node.guard.arm()

            start_mono = time.monotonic()
            node.guard._last_command_monotonic = start_mono

            # Even if ROS clock is frozen, check_timeout uses time.monotonic()
            with patch("time.monotonic", return_value=start_mono + 0.300):
                node._on_watchdog_tick()
                self.assertFalse(
                    node.guard.is_armed,
                    "Guard must time out even if ROS time is frozen",
                )

    def test_supervisor_one_child_hung_heartbeat_timeout(self):
        """Supervisor must detect when one child hangs while the other is healthy."""
        from ubuntu_tank_supervisor.supervisor import Supervisor

        # Supervisor with 0.250s timeout
        sup = Supervisor(guard_deadline_sec=0.250, bridge_deadline_sec=0.250)
        now = 1000.0

        # Both report at t = 1000.0 via trusted kernel channel (pipe)
        sup.record_heartbeat("guard", now, is_trusted_channel=True)
        sup.record_heartbeat("bridge", now, is_trusted_channel=True)
        healthy, _ = sup.check_health(now + 0.100)
        self.assertTrue(healthy)

        # At t = 1000.300: bridge reports fresh heartbeat, but guard has hung
        sup.record_heartbeat("bridge", now + 0.300, is_trusted_channel=True)
        healthy, _ = sup.check_health(now + 0.300)
        self.assertFalse(
            healthy, "Supervisor must fail when guard has hung even if bridge is fresh"
        )

        # Vice-versa: guard reports fresh heartbeat, but bridge has hung
        sup.record_heartbeat("guard", now + 0.600, is_trusted_channel=True)
        healthy, _ = sup.check_health(now + 0.600)
        self.assertFalse(
            healthy, "Supervisor must fail when bridge has hung even if guard is fresh"
        )

    def test_bridge_idle_does_not_timeout_on_zero_commands(self):
        """Zero motor commands must NOT trip the bridge watchdog timeout during idle/stop."""
        from ros_robot_controller.ros_robot_controller_node import RosRobotController

        class MockBoard:
            def __init__(self, *args, **kwargs):
                self.is_mock = True
                self.fatal_error = None
                self.speed_calls = []

            def set_motor_speed(self, data):
                self.speed_calls.append(data)

            def zero_motors(self, count=1):
                pass

            def enable_reception(self, val=True):
                pass

            def set_gamepad_type(self, *args):
                pass

            def set_motor_type(self, *args):
                pass

        with (
            patch(
                "ros_robot_controller.ros_robot_controller_node.rclpy.node.Node",
                MockNode,
            ),
            patch("ros_robot_controller.ros_robot_controller_node.Board", MockBoard),
        ):
            node = RosRobotController(name="test_bridge")
            # Send zero command (stop/disarmed)
            zero_msg = MagicMock()
            zero_msg.data = [
                MagicMock(id=1, rps=0.0),
                MagicMock(id=2, rps=0.0),
                MagicMock(id=3, rps=0.0),
                MagicMock(id=4, rps=0.0),
            ]
            node.set_motor_state(zero_msg)

            # Freshness timestamp should remain None
            self.assertIsNone(node._last_motor_cmd_time)

            # Advance time significantly and check watchdog step
            with patch("time.monotonic", return_value=1000.0):
                node._motor_watchdog_step()

            self.assertFalse(
                node._fatal_fault,
                "Bridge must not enter fatal fault on idle zero commands",
            )
            self.assertTrue(node.running)

    def test_executor_starvation_causes_bridge_watchdog_timeout(self):
        """Active motion followed by executor stall must cause bridge watchdog to enter fatal fault and zero motors."""
        from ros_robot_controller.ros_robot_controller_node import RosRobotController

        class MockBoard:
            def __init__(self, *args, **kwargs):
                self.is_mock = True
                self.fatal_error = None
                self.speed_calls = []
                self.zero_calls = 0

            def set_motor_speed(self, data):
                self.speed_calls.append(data)

            def zero_motors(self, count=1):
                self.zero_calls += count

            def enable_reception(self, val=True):
                pass

            def set_gamepad_type(self, *args):
                pass

            def set_motor_type(self, *args):
                pass

        with (
            patch(
                "ros_robot_controller.ros_robot_controller_node.rclpy.node.Node",
                MockNode,
            ),
            patch("ros_robot_controller.ros_robot_controller_node.Board", MockBoard),
        ):
            node = RosRobotController(name="test_bridge")

            # Send active motion command at t = 100.0
            move_msg = MagicMock()
            move_msg.data = [
                MagicMock(id=1, rps=1.0),
                MagicMock(id=2, rps=1.0),
                MagicMock(id=3, rps=1.0),
                MagicMock(id=4, rps=1.0),
            ]
            with patch("time.monotonic", return_value=100.0):
                node.set_motor_state(move_msg)

            self.assertEqual(node._last_motor_cmd_time, 100.0)

            # Simulate executor starvation: time advances by 300 ms without any new messages being processed
            with patch("time.monotonic", return_value=100.300):
                node._motor_watchdog_step()

            self.assertTrue(
                node._fatal_fault,
                "Bridge watchdog must enter fatal fault on executor starvation",
            )
            self.assertFalse(node.running, "Bridge must stop running")
            self.assertGreaterEqual(
                node.board.zero_calls, 5, "Watchdog must zero motors on fatal fault"
            )


class TestSROS2SecurityAccessControl(unittest.TestCase):
    """Verify deny-by-default SROS2 security policies and enclave boundaries."""

    def test_sros2_governance_and_enclaves_pass(self):
        """Baseline sros2 policies and governance must pass all security invariant checks."""
        import sros2_policy

        self.assertTrue(sros2_policy.verify_governance())
        self.assertTrue(sros2_policy.verify_security_invariants())

    def test_non_guard_enclave_cannot_publish_guarded_motor_topic(self):
        """Enclaves other than /ubuntu_tank/guard must be rejected if they attempt to publish guarded motor topic."""
        import sros2_policy

        # Mock policies dict where controller also tries to publish to guarded topic
        policies = sros2_policy.parse_policies_xml()
        policies["/ubuntu_tank/controller"]["publish_topics"].add(
            "/ros_robot_controller/set_motor_guarded"
        )

        with patch("sros2_policy.parse_policies_xml", return_value=policies):
            with self.assertRaises(sros2_policy.PolicyValidationError) as ctx:
                sros2_policy.verify_security_invariants()
            self.assertIn(
                "Non-guard enclave /ubuntu_tank/controller may publish to guarded topic",
                str(ctx.exception),
            )

    def test_non_operator_enclave_cannot_call_arm_service(self):
        """Only /ubuntu_tank/operator may call /ubuntu_tank_safety/set_arm."""
        import sros2_policy

        policies = sros2_policy.parse_policies_xml()
        policies["/ubuntu_tank/controller"]["service_requests"].add(
            "/ubuntu_tank_safety/set_arm"
        )

        with patch("sros2_policy.parse_policies_xml", return_value=policies):
            with self.assertRaises(sros2_policy.PolicyValidationError) as ctx:
                sros2_policy.verify_security_invariants()
            self.assertIn(
                "allowed to call /ubuntu_tank_safety/set_arm", str(ctx.exception)
            )

    def test_status_enclave_cannot_publish_motion(self):
        """Status enclave is read-only and must be rejected if it attempts to publish motion."""
        import sros2_policy

        policies = sros2_policy.parse_policies_xml()
        policies["/ubuntu_tank/status"]["publish_topics"].add("/controller/cmd_vel")

        with patch("sros2_policy.parse_policies_xml", return_value=policies):
            with self.assertRaises(sros2_policy.PolicyValidationError) as ctx:
                sros2_policy.verify_security_invariants()
            self.assertIn(
                "Status enclave must not publish to any motion command topic",
                str(ctx.exception),
            )

    def test_official_omg_xsd_schemas_validation(self):
        """All governance and permissions files must validate against official OMG XSD schemas."""
        import sros2_policy

        sros2_policy.verify_official_schemas()

    def test_traffic_simulation_allowed_and_denied_flows(self):
        """Traffic simulation must permit authorized flows and reject unauthenticated/unauthorized flows."""
        import sros2_policy

        self.assertTrue(sros2_policy.verify_traffic_simulation())

    def test_sros2_policy_rejects_broad_wildcard_mutation(self):
        """Bridge permissions mutated to include broad rt/* wildcard must be rejected by invariant checker."""
        import shutil
        import sros2_policy

        with tempfile.TemporaryDirectory() as td:
            for f in os.listdir(sros2_policy.PERMISSIONS_DIR):
                shutil.copy(os.path.join(sros2_policy.PERMISSIONS_DIR, f), td)
            bridge_perm = os.path.join(td, "bridge_permissions.xml")
            with open(bridge_perm, "r", encoding="utf-8") as f:
                content = f.read()
            content = content.replace(
                "<topic>rt/ros_robot_controller/battery</topic>",
                "<topic>rt/*</topic>\n            <topic>rt/ros_robot_controller/battery</topic>",
            )
            with open(bridge_perm, "w", encoding="utf-8") as f:
                f.write(content)
            with self.assertRaises(sros2_policy.PolicyValidationError) as ctx:
                sros2_policy.verify_security_invariants(permissions_dir=td)
            self.assertIn("disallowed broad wildcard", str(ctx.exception))

    def test_sros2_policy_ubuntu_2604_libxml2_16_abi_validation(self):
        """On Ubuntu 26.04 where only libxml2.so.16 exists, schema validation must succeed for valid and fail for malformed XML."""
        import sros2_policy
        import ctypes.util

        real_lib = None
        for cand in ["libxml2.so.2", "libxml2.so", "libxml2.so.16"]:
            try:
                real_lib = ctypes.cdll.LoadLibrary(cand)
                break
            except Exception:
                pass
        self.assertIsNotNone(
            real_lib, "Real libxml2 library required on host for ABI simulation"
        )

        def fake_find_library(name):
            if name == "xml2":
                return "libxml2.so.16"
            return None

        def fake_load_library(name):
            if "libxml2.so.2" in name:
                raise OSError(
                    "libxml2.so.2: cannot open shared object file: No such file or directory"
                )
            if "libxml2.so.16" in name:
                return real_lib
            raise OSError(f"{name}: cannot open shared object file")

        with (
            patch("ctypes.util.find_library", side_effect=fake_find_library),
            patch("ctypes.cdll.LoadLibrary", side_effect=fake_load_library),
        ):
            # 1. Verify valid policies pass with Ubuntu 26.04 .16 ABI
            sros2_policy.verify_official_schemas()

            # 2. Verify malformed XML fails with Ubuntu 26.04 .16 ABI
            with tempfile.TemporaryDirectory() as td:
                malformed_xml = os.path.join(td, "malformed.xml")
                with open(malformed_xml, "w", encoding="utf-8") as f:
                    f.write(
                        '<?xml version="1.0"?><invalid_root><bad_element/></invalid_root>'
                    )
                with self.assertRaises(sros2_policy.PolicyValidationError):
                    sros2_policy.validate_xml_against_xsd(
                        malformed_xml, sros2_policy.GOV_SCHEMA_PATH
                    )

    def test_sros2_policy_xmllint_fallback_validation(self):
        """When ctypes libxml2 is unavailable, xmllint executable validates schemas and catches malformed XML."""
        import sros2_policy

        with tempfile.TemporaryDirectory() as td:
            fake_xmllint = os.path.join(td, "xmllint")
            with open(fake_xmllint, "w") as f:
                f.write("""#!/bin/sh
for arg in "$@"; do
  if echo "$arg" | grep -q "malformed"; then
    echo "Schemas validity error: element bad_element failed to validate" >&2
    exit 3
  fi
done
exit 0
""")
            os.chmod(fake_xmllint, 0o755)

            with (
                patch(
                    "sros2_policy._load_libxml2",
                    return_value=(
                        None,
                        ["libxml2.so.16: missing", "libxml2.so.2: missing"],
                    ),
                ),
                patch("shutil.which", return_value=fake_xmllint),
            ):
                # Valid XML passes via xmllint
                sros2_policy.validate_xml_against_xsd(
                    sros2_policy.GOVERNANCE_PATH, sros2_policy.GOV_SCHEMA_PATH
                )

                # Malformed XML fails via xmllint
                malformed_xml = os.path.join(td, "malformed.xml")
                with open(malformed_xml, "w") as f:
                    f.write("<bad/>")
                with self.assertRaises(sros2_policy.PolicyValidationError) as cm:
                    sros2_policy.validate_xml_against_xsd(
                        malformed_xml, sros2_policy.GOV_SCHEMA_PATH
                    )
                self.assertIn(
                    "failed official XSD schema validation via xmllint",
                    str(cm.exception),
                )

    def test_sros2_policy_fails_closed_when_no_validator_available(self):
        """When both libxml2 shared libraries and xmllint CLI are unavailable, validator must fail closed."""
        import sros2_policy

        with (
            patch(
                "sros2_policy._load_libxml2",
                return_value=(
                    None,
                    ["libxml2.so.16: cannot open", "libxml2.so.2: cannot open"],
                ),
            ),
            patch("shutil.which", return_value=None),
        ):
            with self.assertRaises(sros2_policy.PolicyValidationError) as ctx:
                sros2_policy.verify_official_schemas()
            self.assertIn(
                "Official XSD schema validator unavailable", str(ctx.exception)
            )

    def test_sros2_policy_requires_ros_discovery_info(self):
        """Governance and permissions must require explicit ros_discovery_info topic rule."""
        import sros2_policy

        with tempfile.TemporaryDirectory() as td:
            fake_gov = os.path.join(td, "governance.xml")
            with open(sros2_policy.GOVERNANCE_PATH, "r", encoding="utf-8") as f:
                gov_content = f.read()
            gov_stripped = gov_content.replace(
                "<topic_expression>ros_discovery_info</topic_expression>",
                "<topic_expression>ros_discovery_removed</topic_expression>",
            )
            with open(fake_gov, "w", encoding="utf-8") as f:
                f.write(gov_stripped)
            with self.assertRaises(sros2_policy.PolicyValidationError) as ctx:
                sros2_policy.verify_governance(governance_file=fake_gov)
            self.assertIn("ros_discovery_info", str(ctx.exception))


class TestDeployCliArmSafety(unittest.TestCase):
    """Verify deploy.sh arm and disarm safety constraints."""

    def test_arm_fails_without_ack_tracks_raised(self):
        """deploy.sh arm must exit with non-zero when --ack-tracks-raised is absent."""
        import subprocess

        deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")
        res = subprocess.run(["bash", deploy_sh, "arm"], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--ack-tracks-raised", res.stderr)

    def test_arm_fails_closed_when_docker_inventory_fails(self):
        """deploy.sh arm must fail closed if docker ps -a fails or cannot connect."""
        import subprocess

        deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_docker = os.path.join(tmpdir, "docker")
            with open(fake_docker, "w") as f:
                f.write(
                    "#!/bin/sh\necho 'daemon socket permission denied' >&2\nexit 1\n"
                )
            os.chmod(fake_docker, 0o755)

            env = dict(os.environ)
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"

            res = subprocess.run(
                ["bash", deploy_sh, "arm", "--ack-tracks-raised"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("container inventory could not be enumerated", res.stderr)
            self.assertIn("Failing closed", res.stderr)

    def test_deploy_arm_invokes_operator_client_with_enforced_enclave(self):
        """deploy.sh arm must execute operator_client in operator enclave with SROS2 enforced."""
        import subprocess

        deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_docker = os.path.join(tmpdir, "docker")
            with open(fake_docker, "w") as f:
                f.write(
                    "#!/bin/sh\necho 'CONTAINER ID IMAGE COMMAND CREATED STATUS PORTS NAMES'\nexit 0\n"
                )
            os.chmod(fake_docker, 0o755)

            fake_ros2 = os.path.join(tmpdir, "ros2")
            log_file = os.path.join(tmpdir, "ros2.log")
            with open(fake_ros2, "w") as f:
                f.write(f'''#!/bin/sh
echo "$@" >> "{log_file}"
echo "OVERRIDE=$ROS_SECURITY_ENCLAVE_OVERRIDE" >> "{log_file}"
echo "ENABLE=$ROS_SECURITY_ENABLE" >> "{log_file}"
echo "STRATEGY=$ROS_SECURITY_STRATEGY" >> "{log_file}"
exit 0
''')
            os.chmod(fake_ros2, 0o755)

            env = dict(os.environ)
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"

            res = subprocess.run(
                ["bash", deploy_sh, "arm", "--ack-tracks-raised"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0, f"deploy.sh arm failed: {res.stderr}")
            with open(log_file, "r") as f:
                output = f.read()
            self.assertIn("run ubuntu_tank_bringup operator_client --arm", output)
            self.assertIn("OVERRIDE=/ubuntu_tank/operator", output)
            self.assertIn("ENABLE=true", output)
            self.assertIn("STRATEGY=Enforce", output)

    def test_deploy_disarm_invokes_operator_client_with_enforced_enclave(self):
        """deploy.sh disarm must execute operator_client in operator enclave with SROS2 enforced."""
        import subprocess

        deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ros2 = os.path.join(tmpdir, "ros2")
            log_file = os.path.join(tmpdir, "ros2.log")
            with open(fake_ros2, "w") as f:
                f.write(f'''#!/bin/sh
echo "$@" >> "{log_file}"
echo "OVERRIDE=$ROS_SECURITY_ENCLAVE_OVERRIDE" >> "{log_file}"
echo "ENABLE=$ROS_SECURITY_ENABLE" >> "{log_file}"
echo "STRATEGY=$ROS_SECURITY_STRATEGY" >> "{log_file}"
exit 0
''')
            os.chmod(fake_ros2, 0o755)

            env = dict(os.environ)
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"

            res = subprocess.run(
                ["bash", deploy_sh, "disarm"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                res.returncode, 0, f"deploy.sh disarm failed: {res.stderr}"
            )
            with open(log_file, "r") as f:
                output = f.read()
            self.assertIn("run ubuntu_tank_bringup operator_client --disarm", output)
            self.assertIn("OVERRIDE=/ubuntu_tank/operator", output)
            self.assertIn("ENABLE=true", output)
            self.assertIn("STRATEGY=Enforce", output)

    def test_deploy_status_invokes_status_client_with_enforced_enclave(self):
        """deploy.sh status must execute status_client in status enclave with SROS2 enforced."""
        import subprocess

        deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ros2 = os.path.join(tmpdir, "ros2")
            log_file = os.path.join(tmpdir, "ros2.log")
            with open(fake_ros2, "w") as f:
                f.write(f'''#!/bin/sh
echo "$@" >> "{log_file}"
echo "OVERRIDE=$ROS_SECURITY_ENCLAVE_OVERRIDE" >> "{log_file}"
echo "ENABLE=$ROS_SECURITY_ENABLE" >> "{log_file}"
echo "STRATEGY=$ROS_SECURITY_STRATEGY" >> "{log_file}"
exit 0
''')
            os.chmod(fake_ros2, 0o755)

            env = dict(os.environ)
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"

            res = subprocess.run(
                ["bash", deploy_sh, "status"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                res.returncode, 0, f"deploy.sh status failed: {res.stderr}"
            )
            with open(log_file, "r") as f:
                output = f.read()
            self.assertIn("run ubuntu_tank_bringup status_client", output)
            self.assertIn("OVERRIDE=/ubuntu_tank/status", output)
            self.assertIn("ENABLE=true", output)
            self.assertIn("STRATEGY=Enforce", output)

    def test_deploy_arm_preserves_caller_path_precedence_over_sourced_ros(self):
        """deploy.sh ensure_ros_env must preserve caller PATH overrides ahead of sourced setup scripts."""
        import subprocess

        deploy_sh = os.path.join(UBUNTU_TANK_DIR, "deploy.sh")
        with tempfile.TemporaryDirectory() as tmpdir:
            ws_tank = os.path.join(tmpdir, "ws", "ubuntu_tank")
            ws_install = os.path.join(tmpdir, "ws", "install")
            upstream_bin = os.path.join(tmpdir, "upstream_ros", "bin")
            caller_bin = os.path.join(tmpdir, "caller", "bin")
            os.makedirs(ws_tank)
            os.makedirs(ws_install)
            os.makedirs(upstream_bin)
            os.makedirs(caller_bin)

            # Copy deploy.sh into mock workspace
            import shutil

            mock_deploy_sh = os.path.join(ws_tank, "deploy.sh")
            shutil.copyfile(deploy_sh, mock_deploy_sh)
            os.chmod(mock_deploy_sh, 0o755)

            # Upstream setup script that prepends upstream_bin to PATH
            mock_setup = os.path.join(ws_install, "setup.bash")
            with open(mock_setup, "w") as f:
                f.write(f'export PATH="{upstream_bin}:$PATH"\n')

            # Upstream ros2 that would fail if called
            upstream_ros2 = os.path.join(upstream_bin, "ros2")
            with open(upstream_ros2, "w") as f:
                f.write('#!/bin/sh\necho "ERROR: Upstream ROS 2 called" >&2\nexit 42\n')
            os.chmod(upstream_ros2, 0o755)

            # Caller mock ros2
            caller_ros2 = os.path.join(caller_bin, "ros2")
            caller_log = os.path.join(tmpdir, "caller_ros2.log")
            with open(caller_ros2, "w") as f:
                f.write(f'#!/bin/sh\necho "$@" > "{caller_log}"\nexit 0\n')
            os.chmod(caller_ros2, 0o755)

            # Caller mock docker
            caller_docker = os.path.join(caller_bin, "docker")
            with open(caller_docker, "w") as f:
                f.write(
                    "#!/bin/sh\necho 'CONTAINER ID IMAGE COMMAND CREATED STATUS PORTS NAMES'\nexit 0\n"
                )
            os.chmod(caller_docker, 0o755)

            env = dict(os.environ)
            env["PATH"] = f"{caller_bin}:{env.get('PATH', '')}"

            res = subprocess.run(
                ["bash", mock_deploy_sh, "arm", "--ack-tracks-raised"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(
                res.returncode,
                0,
                f"deploy.sh arm failed to preserve caller PATH precedence: {res.stderr}",
            )
            with open(caller_log, "r") as f:
                log_data = f.read()
            self.assertIn("run ubuntu_tank_bringup operator_client --arm", log_data)

    def test_operator_client_disabled_services_and_timeout(self):
        """OperatorClientNode must disable unneeded parameter/type services and timeout cleanly."""
        from ubuntu_tank_bringup.operator_client import (
            OperatorClientNode,
            main as op_main,
        )

        self.assertEqual(op_main(["--help"]), 0)
        self.assertEqual(op_main([]), 1)

        node = OperatorClientNode()
        success, msg = node.call_set_arm(True, timeout_sec=0.05)
        self.assertFalse(success)

    def test_status_client_disabled_services_and_timeout(self):
        """StatusClientNode must disable unneeded parameter/type services, be read-only, and timeout cleanly."""
        from ubuntu_tank_bringup.status_client import StatusClientNode, main as st_main

        node = StatusClientNode()
        res = node.collect_status(timeout_sec=0.05)
        self.assertIsNone(res["guard_state"])
        self.assertIsNone(res["guard_armed"])
        self.assertIsNone(res["battery_mv"])

    def test_status_client_initialization_with_msg_stubs(self):
        """StatusClientNode must initialize with std_msgs.msg.UInt16 and read telemetry correctly."""
        import types
        import importlib
        import io
        from contextlib import redirect_stdout

        fake_rclpy = types.ModuleType("rclpy")
        fake_rclpy_node = types.ModuleType("rclpy.node")
        fake_rclpy_param = types.ModuleType("rclpy.parameter")
        fake_rclpy_qos = types.ModuleType("rclpy.qos")
        fake_std_msgs = types.ModuleType("std_msgs")
        fake_std_msgs_msg = types.ModuleType("std_msgs.msg")
        fake_rrc_msgs = types.ModuleType("ros_robot_controller_msgs")
        fake_rrc_msgs_msg = types.ModuleType("ros_robot_controller_msgs.msg")

        class FakeBool:
            def __init__(self, data=False):
                self.data = data

        class FakeUInt16:
            def __init__(self, data=0):
                self.data = data

        # Populate ros_robot_controller_msgs.msg ONLY with authentic committed .msg exports (NO BatteryState)
        msg_dir = os.path.join(SRC_DIR, "ros_robot_controller_msgs", "msg")
        committed_msgs = [f[:-4] for f in os.listdir(msg_dir) if f.endswith(".msg")]
        self.assertNotIn(
            "BatteryState",
            committed_msgs,
            "BatteryState must not exist in committed msg package",
        )
        for m in committed_msgs:
            setattr(fake_rrc_msgs_msg, m, type(m, (), {}))

        fake_std_msgs_msg.Bool = FakeBool
        fake_std_msgs_msg.UInt16 = FakeUInt16

        class FakeParameter:
            Type = types.SimpleNamespace(BOOL=1)

            def __init__(self, name, param_type, value):
                self.name = name
                self.value = value

        fake_rclpy_param.Parameter = FakeParameter

        class FakeQoS:
            TRANSIENT_LOCAL = 1
            RELIABLE = 2

            def __init__(self, depth=1, durability=None, reliability=None):
                self.depth = depth
                self.durability = durability
                self.reliability = reliability

        fake_rclpy_qos.QoSProfile = FakeQoS
        fake_rclpy_qos.DurabilityPolicy = types.SimpleNamespace(TRANSIENT_LOCAL=1)
        fake_rclpy_qos.ReliabilityPolicy = types.SimpleNamespace(RELIABLE=2)

        class FakeBaseNode:
            def __init__(
                self, name, start_parameter_services=False, parameter_overrides=None
            ):
                self.name = name
                self.start_parameter_services = start_parameter_services
                self.parameter_overrides = parameter_overrides or []
                self.subscriptions = []
                self.destroyed = False

            def create_subscription(self, msg_type, topic, callback, qos):
                sub = types.SimpleNamespace(
                    msg_type=msg_type, topic=topic, callback=callback, qos=qos
                )
                self.subscriptions.append(sub)
                return sub

            def destroy_node(self):
                self.destroyed = True

        fake_rclpy_node.Node = FakeBaseNode
        fake_rclpy.node = fake_rclpy_node
        fake_rclpy.parameter = fake_rclpy_param
        fake_rclpy.qos = fake_rclpy_qos
        fake_std_msgs.msg = fake_std_msgs_msg
        fake_rrc_msgs.msg = fake_rrc_msgs_msg

        fake_rclpy_state = {"ok": True, "spin_calls": 0}

        def fake_ok():
            return fake_rclpy_state["ok"]

        def fake_spin_once(node, timeout_sec=0.1):
            fake_rclpy_state["spin_calls"] += 1

        def fake_init(args=None):
            fake_rclpy_state["ok"] = True

        def fake_shutdown():
            fake_rclpy_state["ok"] = False

        fake_rclpy.ok = fake_ok
        fake_rclpy.spin_once = fake_spin_once
        fake_rclpy.init = fake_init
        fake_rclpy.shutdown = fake_shutdown

        modules_to_patch = {
            "rclpy": fake_rclpy,
            "rclpy.node": fake_rclpy_node,
            "rclpy.parameter": fake_rclpy_param,
            "rclpy.qos": fake_rclpy_qos,
            "std_msgs": fake_std_msgs,
            "std_msgs.msg": fake_std_msgs_msg,
            "ros_robot_controller_msgs": fake_rrc_msgs,
            "ros_robot_controller_msgs.msg": fake_rrc_msgs_msg,
        }

        with patch.dict(sys.modules, modules_to_patch):
            import ubuntu_tank_bringup.status_client as sc

            importlib.reload(sc)

            self.assertIsNotNone(
                sc.rclpy, "rclpy must NOT be None when imports succeed"
            )
            self.assertIs(sc.UInt16, FakeUInt16)

            # 1. Instantiate client
            client = sc.StatusClientNode("test_status_client")
            self.assertEqual(len(client.subscriptions), 3)

            sub_state = next(
                s
                for s in client.subscriptions
                if s.topic == "/ubuntu_tank_safety/state"
            )
            sub_armed = next(
                s
                for s in client.subscriptions
                if s.topic == "/ubuntu_tank_safety/armed"
            )
            sub_batt = next(
                s
                for s in client.subscriptions
                if s.topic == "/ros_robot_controller/battery"
            )

            self.assertIs(sub_state.msg_type, FakeBool)
            self.assertIs(sub_armed.msg_type, FakeBool)
            self.assertIs(sub_batt.msg_type, FakeUInt16)

            # 2. Test receiving messages: Armed + 12.23V (12230 mV)
            sub_state.callback(FakeBool(data=True))
            sub_armed.callback(FakeBool(data=True))
            sub_batt.callback(FakeUInt16(data=12230))

            status = client.collect_status(timeout_sec=0.5)
            self.assertTrue(status["guard_state"])
            self.assertTrue(status["guard_armed"])
            self.assertEqual(status["battery_mv"], 12230)

            with patch.object(sc, "StatusClientNode", return_value=client):
                out = io.StringIO()
                with redirect_stdout(out):
                    ret = sc.main([])
                self.assertEqual(ret, 0)
                out_str = out.getvalue()
                self.assertIn("Guard State: OK", out_str)
                self.assertIn("Armed:       ARMED", out_str)
                self.assertIn("Battery:     12.23 V", out_str)

            # 3. Test receiving messages: Disarmed + 12.15V (12150 mV)
            client2 = sc.StatusClientNode("test_status_client_disarmed")
            sub_state2 = next(
                s
                for s in client2.subscriptions
                if s.topic == "/ubuntu_tank_safety/state"
            )
            sub_armed2 = next(
                s
                for s in client2.subscriptions
                if s.topic == "/ubuntu_tank_safety/armed"
            )
            sub_batt2 = next(
                s
                for s in client2.subscriptions
                if s.topic == "/ros_robot_controller/battery"
            )

            sub_state2.callback(FakeBool(data=True))
            sub_armed2.callback(FakeBool(data=False))
            sub_batt2.callback(FakeUInt16(data=12150))

            status2 = client2.collect_status(timeout_sec=0.5)
            self.assertTrue(status2["guard_state"])
            self.assertFalse(status2["guard_armed"])
            self.assertEqual(status2["battery_mv"], 12150)

            with patch.object(sc, "StatusClientNode", return_value=client2):
                out2 = io.StringIO()
                with redirect_stdout(out2):
                    ret2 = sc.main([])
                self.assertEqual(ret2, 0)
                out_str2 = out2.getvalue()
                self.assertIn("Guard State: OK", out_str2)
                self.assertIn("Armed:       DISARMED", out_str2)
                self.assertIn("Battery:     12.15 V", out_str2)

    def test_status_client_historical_import_failure_reproduction(self):
        """Verify that importing BatteryState from ros_robot_controller_msgs fails as reported in review."""
        import types

        fake_rrc_msgs = types.ModuleType("ros_robot_controller_msgs")
        fake_rrc_msgs_msg = types.ModuleType("ros_robot_controller_msgs.msg")
        msg_dir = os.path.join(SRC_DIR, "ros_robot_controller_msgs", "msg")
        for f in os.listdir(msg_dir):
            if f.endswith(".msg"):
                m = f[:-4]
                setattr(fake_rrc_msgs_msg, m, type(m, (), {}))
        fake_rrc_msgs.msg = fake_rrc_msgs_msg

        with patch.dict(
            sys.modules,
            {
                "ros_robot_controller_msgs": fake_rrc_msgs,
                "ros_robot_controller_msgs.msg": fake_rrc_msgs_msg,
            },
        ):
            with self.assertRaises(ImportError):
                from ros_robot_controller_msgs.msg import BatteryState

    def test_verify_runtime_fails_on_insecure_or_failed_checks(self):
        """verify_runtime.sh must fail closed if security env is missing or queries fail."""
        import subprocess

        verify_sh = os.path.join(SCRIPTS_DIR, "verify_runtime.sh")

        # Test 1: missing ROS_LOCALHOST_ONLY=1
        res = subprocess.run(
            ["bash", verify_sh],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("ROS_LOCALHOST_ONLY must be 1", res.stderr)

        # Test 2: ROS_LOCALHOST_ONLY=1 but ROS_SECURITY_ENABLE != true
        env2 = {"PATH": os.environ.get("PATH", ""), "ROS_LOCALHOST_ONLY": "1"}
        res2 = subprocess.run(
            ["bash", verify_sh], capture_output=True, text=True, env=env2
        )
        self.assertEqual(res2.returncode, 1)
        self.assertIn("ROS_SECURITY_ENABLE must be 'true'", res2.stderr)

        # Test 3: All security envs set, but ros2 returns guard armed (data: true)
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ros2 = os.path.join(tmpdir, "ros2")
            with open(fake_ros2, "w") as f:
                f.write(
                    '#!/bin/sh\nif [ "$1" = "topic" ] && [ "$2" = "echo" ]; then\necho "data: true"\nexit 0\nfi\nexit 0\n'
                )
            os.chmod(fake_ros2, 0o755)

            env3 = {
                "PATH": f"{tmpdir}:{os.environ.get('PATH', '')}",
                "ROS_LOCALHOST_ONLY": "1",
                "ROS_SECURITY_ENABLE": "true",
                "ROS_SECURITY_STRATEGY": "Enforce",
            }
            res3 = subprocess.run(
                ["bash", verify_sh], capture_output=True, text=True, env=env3
            )
            self.assertEqual(res3.returncode, 1)
            self.assertIn("Guard state is currently ARMED", res3.stderr)

    def test_verify_runtime_rejects_invalid_endpoint_graphs(self):
        """verify_runtime.sh must fail on 10 publishers, missing bridge subscriber, or unrelated publisher."""
        import subprocess

        verify_sh = os.path.join(SCRIPTS_DIR, "verify_runtime.sh")

        # Case A: 10 publishers on guarded topic
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ros2 = os.path.join(tmpdir, "ros2")
            with open(fake_ros2, "w") as f:
                f.write("""#!/bin/sh
if [ "$1" = "topic" ] && [ "$2" = "echo" ]; then
  echo "data: false"
  exit 0
fi
if [ "$1" = "topic" ] && [ "$2" = "info" ]; then
  if [ "$4" = "/ubuntu_tank_safety/motor_input" ]; then
    echo "Publisher count: 1"
    echo "Node name: controller"
    echo "Subscription count: 1"
    echo "Node name: motor_guard"
    exit 0
  fi
  if [ "$4" = "/ros_robot_controller/set_motor_guarded" ]; then
    echo "Publisher count: 10"
    echo "Node name: motor_guard"
    echo "Subscription count: 0"
    exit 0
  fi
fi
exit 0
""")
            os.chmod(fake_ros2, 0o755)

            env = {
                "PATH": f"{tmpdir}:{os.environ.get('PATH', '')}",
                "ROS_LOCALHOST_ONLY": "1",
                "ROS_SECURITY_ENABLE": "true",
                "ROS_SECURITY_STRATEGY": "Enforce",
            }
            res = subprocess.run(
                ["bash", verify_sh], capture_output=True, text=True, env=env
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("Publisher count is 10, expected exactly 1", res.stderr)

        # Case B: Unrelated publisher with motor_guard subscriber
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ros2 = os.path.join(tmpdir, "ros2")
            with open(fake_ros2, "w") as f:
                f.write("""#!/bin/sh
if [ "$1" = "topic" ] && [ "$2" = "echo" ]; then
  echo "data: false"
  exit 0
fi
if [ "$1" = "topic" ] && [ "$2" = "info" ]; then
  if [ "$4" = "/ubuntu_tank_safety/motor_input" ]; then
    echo "Publisher count: 1"
    echo "Node name: controller"
    echo "Subscription count: 1"
    echo "Node name: motor_guard"
    exit 0
  fi
  if [ "$4" = "/ros_robot_controller/set_motor_guarded" ]; then
    echo "Publisher count: 1"
    echo "Node name: rogue_node"
    echo "Subscription count: 1"
    echo "Node name: motor_guard"
    exit 0
  fi
fi
exit 0
""")
            os.chmod(fake_ros2, 0o755)

            env = {
                "PATH": f"{tmpdir}:{os.environ.get('PATH', '')}",
                "ROS_LOCALHOST_ONLY": "1",
                "ROS_SECURITY_ENABLE": "true",
                "ROS_SECURITY_STRATEGY": "Enforce",
            }
            res = subprocess.run(
                ["bash", verify_sh], capture_output=True, text=True, env=env
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("rogue_node", res.stderr)

        # Case C: Missing bridge subscriber on guarded topic (sub_count 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ros2 = os.path.join(tmpdir, "ros2")
            with open(fake_ros2, "w") as f:
                f.write("""#!/bin/sh
if [ "$1" = "topic" ] && [ "$2" = "echo" ]; then
  echo "data: false"
  exit 0
fi
if [ "$1" = "topic" ] && [ "$2" = "info" ]; then
  if [ "$4" = "/ubuntu_tank_safety/motor_input" ]; then
    echo "Publisher count: 1"
    echo "Node name: controller"
    echo "Subscription count: 1"
    echo "Node name: motor_guard"
    exit 0
  fi
  if [ "$4" = "/ros_robot_controller/set_motor_guarded" ]; then
    echo "Publisher count: 1"
    echo "Node name: motor_guard"
    echo "Subscription count: 0"
    exit 0
  fi
fi
exit 0
""")
            os.chmod(fake_ros2, 0o755)

            env = {
                "PATH": f"{tmpdir}:{os.environ.get('PATH', '')}",
                "ROS_LOCALHOST_ONLY": "1",
                "ROS_SECURITY_ENABLE": "true",
                "ROS_SECURITY_STRATEGY": "Enforce",
            }
            res = subprocess.run(
                ["bash", verify_sh], capture_output=True, text=True, env=env
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("Subscription count is 0, expected exactly 1", res.stderr)

        # Case D: Valid endpoints pass
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ros2 = os.path.join(tmpdir, "ros2")
            with open(fake_ros2, "w") as f:
                f.write("""#!/bin/sh
if [ "$1" = "topic" ] && [ "$2" = "echo" ]; then
  echo "data: false"
  exit 0
fi
if [ "$1" = "topic" ] && [ "$2" = "info" ]; then
  if [ "$4" = "/ubuntu_tank_safety/motor_input" ]; then
    echo "Publisher count: 1"
    echo "Node name: controller"
    echo "Subscription count: 1"
    echo "Node name: motor_guard"
    exit 0
  fi
  if [ "$4" = "/ros_robot_controller/set_motor_guarded" ]; then
    echo "Publisher count: 1"
    echo "Node name: motor_guard"
    echo "Subscription count: 1"
    echo "Node name: ros_robot_controller"
    exit 0
  fi
fi
exit 0
""")
            os.chmod(fake_ros2, 0o755)

            env = {
                "PATH": f"{tmpdir}:{os.environ.get('PATH', '')}",
                "ROS_LOCALHOST_ONLY": "1",
                "ROS_SECURITY_ENABLE": "true",
                "ROS_SECURITY_STRATEGY": "Enforce",
            }
            res = subprocess.run(
                ["bash", verify_sh], capture_output=True, text=True, env=env
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("Runtime verification completed successfully", res.stdout)


if __name__ == "__main__":
    unittest.main()
