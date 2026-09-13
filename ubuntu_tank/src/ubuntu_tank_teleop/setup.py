"""
Install the ubuntu_tank_teleop Python package and its supported ROS entry points.
"""

from setuptools import setup

package_name = "ubuntu_tank_teleop"

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
    description="Safe keyboard teleoperation with renewable motion leases and fail-closed zeroing",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "teleop_key = ubuntu_tank_teleop.teleop_key_node:main",
        ],
    },
)
