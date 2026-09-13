#!/bin/bash
# Retain the vendor device setup as reference only. Native installation uses deploy.sh
# and host/99-mentorpi-rrc.rules instead.
# Adapted from Hiwonder MentorPi; prefix the rules glob to avoid option parsing:
# mentorpi/src/driver/ros_robot_controller/scripts/create_udev_rules.sh.

sudo cp ./*.rules /etc/udev/rules.d
echo " "
echo "Restarting udev"
echo ""
sudo service udev reload
sudo service udev restart
echo "finish "
