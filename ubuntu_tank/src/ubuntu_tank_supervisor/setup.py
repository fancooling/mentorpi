"""
Install the ubuntu_tank_supervisor Python package and its supported ROS entry points.
"""

from setuptools import setup

package_name = "ubuntu_tank_supervisor"

setup(
    name=package_name,
    version="1.0.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools", "ubuntu_tank_protocol"],
    zip_safe=True,
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="Robot heartbeat supervision and restricted container lifecycle",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "supervisor = ubuntu_tank_supervisor.supervisor_node:main",
        ],
    },
)
