# Define the vendor PWMServoState ROS message; retain its field layout for board
# interface compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/PWMServoState.msg.
uint16[] id
uint16[] position
int16[] offset
