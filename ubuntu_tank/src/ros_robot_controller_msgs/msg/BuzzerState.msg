# Define the vendor BuzzerState ROS message; retain its field layout for board interface
# compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/BuzzerState.msg.
uint16 freq
float32 on_time
float32 off_time
uint16 repeat
