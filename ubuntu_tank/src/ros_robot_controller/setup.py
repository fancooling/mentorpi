"""
Install the ros_robot_controller Python package and its supported ROS entry points.

Adapted/refactored from Hiwonder MentorPi:

mentorpi/src/driver/ros_robot_controller/setup.py.

Local adaptations: Synchronize package version (1.0.0), maintainer dev@mentorpi.local,
description, and Apache-2.0 license declaration with package.xml.
"""

import os
from glob import glob
from setuptools import setup

package_name = "ros_robot_controller"

setup(
    name=package_name,
    version="1.0.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (
            os.path.join("share", package_name, "launch"),
            glob(os.path.join("launch", "*.*")),
        ),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="MentorPi STM32 hardware bridge node adapted for native Ubuntu 26.04",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "ros_robot_controller = ros_robot_controller.ros_robot_controller_node:main",
        ],
    },
)
