# Define the vendor ServoState ROS message; retain its field layout for board interface
# compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/ServoState.msg.
int32 id            #
int32 position      # current position (in encoder units)
