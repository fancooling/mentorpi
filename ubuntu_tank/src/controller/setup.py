"""
Install the controller Python package and its supported ROS entry points.

Adapted/refactored from Hiwonder MentorPi:

mentorpi/src/driver/controller/setup.py.

Local adaptations: Export only odom_publisher console script; omit legacy launch and
config files; synchronize version (1.0.0), maintainer dev@mentorpi.local, and Apache-2.0
license with package.xml.
"""

import os
from setuptools import setup

package_name = "controller"

setup(
    name=package_name,
    version="1.0.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="MentorPi tank motion controller core (adapted for native Ubuntu 26.04)",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "odom_publisher = controller.odom_publisher_node:main",
        ],
    },
)
