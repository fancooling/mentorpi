import os
from glob import glob
from setuptools import setup

package_name = "ubuntu_tank_bringup"

setup(
    name=package_name,
    version="1.0.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="Guarded launch and bringup integration for Ubuntu Tank native controller",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "operator_client = ubuntu_tank_bringup.operator_client:main",
            "status_client = ubuntu_tank_bringup.status_client:main",
            "bench_client = ubuntu_tank_bringup.bench_client:main",
        ],
    },
)
