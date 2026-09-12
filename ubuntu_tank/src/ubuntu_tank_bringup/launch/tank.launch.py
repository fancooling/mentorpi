"""Guarded bringup launch for Ubuntu Tank native controller.

Launches:
1. controller (odom_publisher)
2. ubuntu_tank_safety (motor_guard)
3. ros_robot_controller (ros_robot_controller)

Enforces:
- Explicit topic remappings ensuring commands pass ONLY through the motor guard:
    /controller/cmd_vel -> controller -> /ubuntu_tank_safety/motor_input
    /ubuntu_tank_safety/motor_input -> motor_guard -> /ros_robot_controller/set_motor_guarded
    /ros_robot_controller/set_motor_guarded -> ros_robot_controller -> /dev/rrc
- Fail-closed graph shutdown if guard or hardware bridge exits.
- Disarmed startup by default with conservative speed limits and 250 ms freshness timeout.
- Controller-only mode with all legacy non-motor command endpoints disabled.
"""

import os
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    LogInfo,
    OpaqueFunction,
    RegisterEventHandler,
    SetEnvironmentVariable,
)
from launch.events import Shutdown
from launch.event_handlers import OnProcessExit
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def validate_security_preflight(context, *args, **kwargs):
    """Fail closed if loopback-only discovery or SROS2 security is missing or unenforced."""
    lh = context.launch_configurations.get('localhost_only', '1')
    if str(lh) != '1':
        raise RuntimeError("Safety violation: ROS_LOCALHOST_ONLY must be set to '1' for guarded bringup.")

    sec_en = context.launch_configurations.get('security_enable', 'true').lower()
    if sec_en != 'true':
        raise RuntimeError("Safety violation: ROS_SECURITY_ENABLE must be 'true' for guarded bringup.")

    sec_strat = context.launch_configurations.get('security_strategy', 'Enforce')
    if sec_strat != 'Enforce':
        raise RuntimeError("Safety violation: ROS_SECURITY_STRATEGY must be 'Enforce' for guarded bringup.")

    enforce_keystore = context.launch_configurations.get('enforce_security', 'true').lower()
    keystore = context.launch_configurations.get('security_keystore', '/etc/opt/ubuntu_tank/security/keystore')
    if enforce_keystore == 'true':
        if not os.path.isdir(keystore):
            raise RuntimeError(
                f"Safety violation: SROS2 keystore missing at '{keystore}'. "
                f"Guarded bringup fails closed without valid security credentials."
            )
        for enc in ['controller', 'guard', 'bridge']:
            enc_dir = os.path.join(keystore, 'enclaves', 'ubuntu_tank', enc)
            if not os.path.isdir(enc_dir):
                raise RuntimeError(
                    f"Safety violation: Required enclave credentials missing at '{enc_dir}'"
                )
    return []


def generate_launch_description():
    # Declare launch arguments with conservative and safe defaults
    machine_type = LaunchConfiguration('machine_type')
    controller_only = LaunchConfiguration('controller_only')
    serial_device = LaunchConfiguration('serial_device')
    baud_rate = LaunchConfiguration('baud_rate')
    max_rps = LaunchConfiguration('max_rps')
    guard_timeout_sec = LaunchConfiguration('guard_timeout_sec')
    check_rate_hz = LaunchConfiguration('check_rate_hz')
    guard_heartbeat_interval_sec = LaunchConfiguration('guard_heartbeat_interval_sec')
    wheelbase = LaunchConfiguration('wheelbase')
    track_width = LaunchConfiguration('track_width')
    wheel_diameter = LaunchConfiguration('wheel_diameter')
    pub_odom_topic = LaunchConfiguration('pub_odom_topic')
    linear_correction_factor = LaunchConfiguration('linear_correction_factor')
    angular_correction_factor = LaunchConfiguration('angular_correction_factor')
    left_correction_factor = LaunchConfiguration('left_correction_factor')
    right_correction_factor = LaunchConfiguration('right_correction_factor')
    serial_read_timeout_sec = LaunchConfiguration('serial_read_timeout_sec')
    write_timeout_sec = LaunchConfiguration('write_timeout_sec')
    freshness_timeout_sec = LaunchConfiguration('freshness_timeout_sec')
    serial_silence_timeout_sec = LaunchConfiguration('serial_silence_timeout_sec')

    localhost_only = LaunchConfiguration('localhost_only')
    security_enable = LaunchConfiguration('security_enable')
    security_strategy = LaunchConfiguration('security_strategy')
    security_keystore = LaunchConfiguration('security_keystore')
    enforce_security = LaunchConfiguration('enforce_security')

    # 1. Controller node: converts Twist -> 4-motor speeds, publishing ONLY to guard input
    controller_node = Node(
        package='controller',
        executable='odom_publisher',
        name='controller',
        output='screen',
        ros_arguments=['--enclave', '/ubuntu_tank/controller'],
        parameters=[{
            'machine_type': machine_type,
            'controller_only': controller_only,
            'max_linear_speed': LaunchConfiguration('max_linear_speed'),
            'max_angular_speed': LaunchConfiguration('max_angular_speed'),
            'wheelbase': wheelbase,
            'track_width': track_width,
            'wheel_diameter': wheel_diameter,
            'pub_odom_topic': pub_odom_topic,
            'linear_correction_factor': linear_correction_factor,
            'angular_correction_factor': angular_correction_factor,
            'left_correction_factor': left_correction_factor,
            'right_correction_factor': right_correction_factor,
            'cmd_vel_topic': '/controller/cmd_vel',
            'motor_output_topic': '/ubuntu_tank_safety/motor_input',
        }],
    )

    # 2. Motor guard node: validates 4-motor speeds, checks arming & freshness, outputs to bridge
    motor_guard_node = Node(
        package='ubuntu_tank_safety',
        executable='motor_guard',
        name='motor_guard',
        output='screen',
        ros_arguments=['--enclave', '/ubuntu_tank/guard'],
        parameters=[{
            'max_rps': max_rps,
            'timeout_sec': guard_timeout_sec,
            'check_rate_hz': check_rate_hz,
            'heartbeat_interval_sec': guard_heartbeat_interval_sec,
        }],
    )

    # 3. Hardware serial bridge: communicates with STM32 over /dev/rrc
    # Subscribes to guarded motor topic (/ros_robot_controller/set_motor_guarded)
    bridge_node = Node(
        package='ros_robot_controller',
        executable='ros_robot_controller',
        name='ros_robot_controller',
        output='screen',
        ros_arguments=['--enclave', '/ubuntu_tank/bridge'],
        parameters=[{
            'controller_only': controller_only,
            'machine_type': machine_type,
            'serial_device': serial_device,
            'baud_rate': baud_rate,
            'serial_read_timeout_sec': serial_read_timeout_sec,
            'write_timeout_sec': write_timeout_sec,
            'freshness_timeout_sec': freshness_timeout_sec,
            'serial_silence_timeout_sec': serial_silence_timeout_sec,
            'motor_topic': '/ros_robot_controller/set_motor_guarded',
        }],
        remappings=[
            ('~/set_motor', '/ros_robot_controller/set_motor_guarded'),
        ],
    )

    # Shutdown handlers: if guard, bridge, or controller exits, shut down the complete graph
    on_guard_exit = RegisterEventHandler(
        OnProcessExit(
            target_action=motor_guard_node,
            on_exit=[
                LogInfo(msg="[Bringup] motor_guard exited; shutting down complete controller graph."),
                EmitEvent(event=Shutdown(reason="motor_guard exited")),
            ]
        )
    )

    on_bridge_exit = RegisterEventHandler(
        OnProcessExit(
            target_action=bridge_node,
            on_exit=[
                LogInfo(msg="[Bringup] ros_robot_controller exited; shutting down complete controller graph."),
                EmitEvent(event=Shutdown(reason="ros_robot_controller exited")),
            ]
        )
    )

    on_controller_exit = RegisterEventHandler(
        OnProcessExit(
            target_action=controller_node,
            on_exit=[
                LogInfo(msg="[Bringup] controller exited; shutting down complete controller graph."),
                EmitEvent(event=Shutdown(reason="controller exited")),
            ]
        )
    )

    return LaunchDescription([
        # Security & discovery arguments and environment variables
        DeclareLaunchArgument('localhost_only', default_value='1', description='Enforce loopback-only discovery'),
        DeclareLaunchArgument('security_enable', default_value='true', description='Enable SROS2 security'),
        DeclareLaunchArgument('security_strategy', default_value='Enforce', description='SROS2 enforcement strategy'),
        DeclareLaunchArgument('security_keystore', default_value='/etc/opt/ubuntu_tank/security/keystore', description='SROS2 keystore path'),
        DeclareLaunchArgument('enforce_security', default_value='true', description='Fail closed if keystore credentials missing'),

        SetEnvironmentVariable(name='ROS_LOCALHOST_ONLY', value=localhost_only),
        SetEnvironmentVariable(name='ROS_SECURITY_ENABLE', value=security_enable),
        SetEnvironmentVariable(name='ROS_SECURITY_STRATEGY', value=security_strategy),
        SetEnvironmentVariable(name='ROS_SECURITY_KEYSTORE', value=security_keystore),

        OpaqueFunction(function=validate_security_preflight),

        DeclareLaunchArgument('machine_type', default_value='MentorPi_Tank', description='Chassis model'),
        DeclareLaunchArgument('controller_only', default_value='true', description='Enable controller-only mode'),
        DeclareLaunchArgument('serial_device', default_value='/dev/rrc', description='STM32 serial device path'),
        DeclareLaunchArgument('baud_rate', default_value='1000000', description='STM32 baud rate'),
        DeclareLaunchArgument('max_rps', default_value='2.0', description='Maximum motor revolutions per second'),
        DeclareLaunchArgument('guard_timeout_sec', default_value='0.250', description='Guard command freshness timeout (sec)'),
        DeclareLaunchArgument('max_linear_speed', default_value='0.5', description='Controller linear velocity cap in m/s'),
        DeclareLaunchArgument('max_angular_speed', default_value='2.0', description='Controller angular velocity cap in rad/s'),
        DeclareLaunchArgument('check_rate_hz', default_value='50.0', description='Guard watchdog frequency (Hz)'),
        DeclareLaunchArgument('guard_heartbeat_interval_sec', default_value='0.200', description='Guard supervisor heartbeat interval (sec)'),
        DeclareLaunchArgument('wheelbase', default_value='0.1368', description='Wheelbase in meters'),
        DeclareLaunchArgument('track_width', default_value='0.1446', description='Track width in meters'),
        DeclareLaunchArgument('wheel_diameter', default_value='0.075', description='Wheel diameter in meters'),
        DeclareLaunchArgument('pub_odom_topic', default_value='true', description='Publish odom_raw topic'),
        DeclareLaunchArgument('linear_correction_factor', default_value='1.0', description='Linear correction factor'),
        DeclareLaunchArgument('angular_correction_factor', default_value='1.0', description='Angular correction factor'),
        DeclareLaunchArgument('left_correction_factor', default_value='1.0', description='Left tread correction factor'),
        DeclareLaunchArgument('right_correction_factor', default_value='1.0', description='Right tread correction factor'),
        DeclareLaunchArgument('serial_read_timeout_sec', default_value='0.050', description='Serial read poll timeout (sec)'),
        DeclareLaunchArgument('write_timeout_sec', default_value='0.100', description='Serial write timeout (sec)'),
        DeclareLaunchArgument('freshness_timeout_sec', default_value='0.250', description='Bridge command freshness timeout (sec)'),
        DeclareLaunchArgument('serial_silence_timeout_sec', default_value='0.500', description='Fatal serial silence timeout (sec)'),

        # Nodes
        controller_node,
        motor_guard_node,
        bridge_node,

        # Shutdown event handlers
        on_guard_exit,
        on_bridge_exit,
        on_controller_exit,
    ])
