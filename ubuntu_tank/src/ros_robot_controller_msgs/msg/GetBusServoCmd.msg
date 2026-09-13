# Define the vendor GetBusServoCmd ROS message; retain its field layout for board
# interface compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/GetBusServoCmd.msg.
uint8 id
uint8 get_id
uint8 get_position
uint8 get_offset
uint8 get_voltage
uint8 get_temperature
uint8 get_position_limit
uint8 get_voltage_limit
uint8 get_max_temperature_limit
uint8 get_torque_state
