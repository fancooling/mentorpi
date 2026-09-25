"""Install the web package and explicit ROS package-index/manifest metadata."""

from setuptools import find_packages, setup

package_name = "ubuntu_tank_web"

setup(
    name=package_name,
    version="1.0.0",
    packages=find_packages(),
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=[
        "ubuntu_tank_protocol",
        "fastapi",
        "uvicorn",
        "pydantic",
        "websockets",
        "cryptography",
        "PyYAML",
    ],
    zip_safe=True,
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="FastAPI Web Control API and Uvicorn Server for MentorPi Pi 5",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "mentorpi_tank_web = ubuntu_tank_web.entrypoint:main",
        ],
    },
)
