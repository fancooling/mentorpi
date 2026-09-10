"""Launch file for safe keyboard teleoperation.

Runs ubuntu_tank_teleop/teleop_key with renewable 150 ms command leases,
publishing bounded velocity commands to /controller/cmd_vel.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    linear_vel = LaunchConfiguration('linear_vel')
    angular_vel = LaunchConfiguration('angular_vel')
    lease_duration_sec = LaunchConfiguration('lease_duration_sec')
    publish_rate_hz = LaunchConfiguration('publish_rate_hz')

    localhost_only = LaunchConfiguration('localhost_only')
    security_enable = LaunchConfiguration('security_enable')
    security_strategy = LaunchConfiguration('security_strategy')
    security_keystore = LaunchConfiguration('security_keystore')

    teleop_node = Node(
        package='ubuntu_tank_teleop',
        executable='teleop_key',
        name='teleop_key',
        output='screen',
        emulate_tty=True,
        ros_arguments=['--enclave', '/ubuntu_tank/operator'],
        parameters=[{
            'linear_vel': linear_vel,
            'angular_vel': angular_vel,
            'lease_duration_sec': lease_duration_sec,
            'publish_rate_hz': publish_rate_hz,
        }],
    )

    return LaunchDescription([
        DeclareLaunchArgument('localhost_only', default_value='1', description='Enforce loopback-only discovery'),
        DeclareLaunchArgument('security_enable', default_value='true', description='Enable SROS2 security'),
        DeclareLaunchArgument('security_strategy', default_value='Enforce', description='SROS2 enforcement strategy'),
        DeclareLaunchArgument('security_keystore', default_value='/etc/opt/ubuntu_tank/security/keystore', description='SROS2 keystore path'),

        SetEnvironmentVariable(name='ROS_LOCALHOST_ONLY', value=localhost_only),
        SetEnvironmentVariable(name='ROS_SECURITY_ENABLE', value=security_enable),
        SetEnvironmentVariable(name='ROS_SECURITY_STRATEGY', value=security_strategy),
        SetEnvironmentVariable(name='ROS_SECURITY_KEYSTORE', value=security_keystore),

        DeclareLaunchArgument(
            'linear_vel',
            default_value='0.2',
            description='Linear velocity in m/s'
        ),
        DeclareLaunchArgument(
            'angular_vel',
            default_value='0.5',
            description='Angular velocity in rad/s'
        ),
        DeclareLaunchArgument(
            'lease_duration_sec',
            default_value='0.150',
            description='Command lease duration in seconds (must be < 0.250s)'
        ),
        DeclareLaunchArgument(
            'publish_rate_hz',
            default_value='20.0',
            description='Twist publication rate in Hz'
        ),
        teleop_node,
    ])
