"""Launch the controller-only STM32 bridge with bounded serial writes."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Return configurable bridge launch actions with safe controller defaults."""
    imu_frame = LaunchConfiguration('imu_frame')
    serial_device = LaunchConfiguration('serial_device')
    baud_rate = LaunchConfiguration('baud_rate')
    controller_only = LaunchConfiguration('controller_only')
    machine_type = LaunchConfiguration('machine_type')
    serial_read_timeout_sec = LaunchConfiguration('serial_read_timeout_sec')
    write_timeout_sec = LaunchConfiguration('write_timeout_sec')
    serial_silence_timeout_sec = LaunchConfiguration('serial_silence_timeout_sec')

    return LaunchDescription([
        DeclareLaunchArgument('imu_frame', default_value='imu_link', description='IMU frame ID'),
        DeclareLaunchArgument('serial_device', default_value='/dev/rrc', description='STM32 serial device path'),
        DeclareLaunchArgument('baud_rate', default_value='1000000', description='STM32 serial baud rate'),
        DeclareLaunchArgument('controller_only', default_value='true', description='Enable controller-only mode'),
        DeclareLaunchArgument('machine_type', default_value='MentorPi_Tank', description='Chassis machine type'),
        DeclareLaunchArgument('serial_read_timeout_sec', default_value='0.050', description='STM32 serial read poll timeout in seconds'),
        DeclareLaunchArgument('write_timeout_sec', default_value='0.100', description='STM32 serial write timeout in seconds'),
        DeclareLaunchArgument('serial_silence_timeout_sec', default_value='0.500', description='Fatal timeout for receiving no STM32 bytes'),

        Node(
            package='ros_robot_controller',
            executable='ros_robot_controller',
            output='screen',
            parameters=[{
                'imu_frame': imu_frame,
                'serial_device': serial_device,
                'baud_rate': baud_rate,
                'controller_only': controller_only,
                'machine_type': machine_type,
                'serial_read_timeout_sec': serial_read_timeout_sec,
                'write_timeout_sec': write_timeout_sec,
                'serial_silence_timeout_sec': serial_silence_timeout_sec,
            }]
        )
    ])
