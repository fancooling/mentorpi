# Define the vendor Sbus ROS message; retain its field layout for board interface
# compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/Sbus.msg.
std_msgs/Header header
float32[] channel
