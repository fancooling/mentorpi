"""Install the ubuntu_tank_operator runtime authority package."""

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
    install_requires=["ubuntu_tank_protocol", "ubuntu_tank_supervisor", "setuptools"],
    zip_safe=True,
    maintainer="Ubuntu Tank Maintainers",
    maintainer_email="dev@mentorpi.local",
    description="Operator agent, leases, and arbitration",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "operator_agent = ubuntu_tank_operator.entrypoint:main",
        ],
    },
)
