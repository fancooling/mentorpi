"""Install the ubuntu_tank_camera runtime media package."""

from setuptools import find_packages, setup

package_name = "ubuntu_tank_camera"

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
    install_requires=["ubuntu_tank_protocol", "setuptools"],
    zip_safe=True,
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="Camera acquisition, frame distribution, and media IPC worker",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "camera_worker = ubuntu_tank_camera.entrypoint:main",
        ],
    },
)
