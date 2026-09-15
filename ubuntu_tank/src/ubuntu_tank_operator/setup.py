"""Install the ubuntu_tank_operator Python package and its schemas."""

from setuptools import setup

package_name = "ubuntu_tank_operator"

setup(
    name=package_name,
    version="1.0.0",
    packages=[package_name],
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="Shared operator agent, leases, arbitration, and web control schemas",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [],
    },
)
