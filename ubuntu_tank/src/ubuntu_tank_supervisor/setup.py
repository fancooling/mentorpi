from setuptools import setup

package_name = 'ubuntu_tank_supervisor'

setup(
    name=package_name,
    version='1.0.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Ubuntu Tank Maintainers',
    maintainer_email='dev@mentorpi.local',
    description='Trusted AND-gating process and heartbeat supervisor for systemd watchdog integration',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'supervisor = ubuntu_tank_supervisor.supervisor_node:main',
        ],
    },
)
