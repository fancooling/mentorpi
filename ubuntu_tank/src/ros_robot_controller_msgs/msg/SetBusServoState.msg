# Define the vendor SetBusServoState ROS message; retain its field layout for board
# interface compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/SetBusServoState.msg.
ros_robot_controller_msgs/BusServoState[] state

float64 duration
