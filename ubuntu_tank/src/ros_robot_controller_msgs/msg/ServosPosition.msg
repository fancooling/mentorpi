# Define the vendor ServosPosition ROS message; retain its field layout for board
# interface compatibility.
# Copied directly from Hiwonder MentorPi:
# mentorpi/src/driver/ros_robot_controller_msgs/msg/ServosPosition.msg.
float64 duration
ros_robot_controller_msgs/ServoPosition[] position
