# Define the vendor SetPWMServoState ROS message; retain its field layout for board
# interface compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/SetPWMServoState.msg.
ros_robot_controller_msgs/PWMServoState[] state
float64 duration
