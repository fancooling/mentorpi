# Define the vendor GetPWMServoCmd ROS message; retain its field layout for board
# interface compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/GetPWMServoCmd.msg.
uint8 id
uint8 get_position
uint8 get_offset
