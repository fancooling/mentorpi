"""Install ROS-free message contracts and generation tools for runtime and web."""

from setuptools import setup

setup(
    name="ubuntu_tank_protocol",
    version="1.0.0",
    packages=["ubuntu_tank_protocol"],
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/ubuntu_tank_protocol"],
        ),
        ("share/ubuntu_tank_protocol", ["package.xml"]),
    ],
    install_requires=[],
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="ROS-independent operator and web protocol contracts",
    license="Apache-2.0",
)
