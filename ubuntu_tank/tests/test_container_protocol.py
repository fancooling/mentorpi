"""Exercise C1's installed ROS-free web boundary and generated wire contracts."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]


class TestProtocolExtraction(unittest.TestCase):
    """Verify packaged consumers without the checkout or runtime authority."""

    def test_installed_web_without_runtime(self):
        """Build/install real packages and serve requests with runtime imports forbidden."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheels = root / "wheels"
            for name in ("ubuntu_tank_protocol", "ubuntu_tank_web"):
                source = root / name
                shutil.copytree(
                    WORKSPACE / "src" / name,
                    source,
                    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"),
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "setup.py",
                        "bdist_wheel",
                        "--dist-dir",
                        str(wheels),
                    ],
                    cwd=source,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            installed = root / "installed"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--target",
                    str(installed),
                    *map(str, wheels.glob("*.whl")),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in ("ubuntu_tank_protocol", "ubuntu_tank_web"):
                shutil.rmtree(root / name)
            probe = r"""
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {
            'rclpy', 'geometry_msgs', 'std_msgs', 'std_srvs',
            'ros_robot_controller_msgs', 'ubuntu_tank_operator',
            'ubuntu_tank_bringup', 'ubuntu_tank_supervisor',
        }:
            raise AssertionError('Runtime import attempted: ' + fullname)
sys.meta_path.insert(0, NoRuntime())
from fastapi.testclient import TestClient
from ubuntu_tank_protocol.config import WebControlConfig
from ubuntu_tank_protocol.schemas import VersionResponse
from ubuntu_tank_protocol.openapi_generator import generate_openapi_spec
from ubuntu_tank_web.app import create_app
from ubuntu_tank_web.lifecycle_client import LifecycleClient
from ubuntu_tank_web import entrypoint
config = WebControlConfig(operator_socket_path='/nonexistent/operator.sock',
                          lifecycle_socket_path='/nonexistent/lifecycle.sock')
config.validate()
app = create_app(config=config)
with TestClient(app) as client:
    response = client.get('/api/v1/version')
    assert response.status_code == 200, response.text
    assert response.json()['protocol_version'] == VersionResponse().protocol_version
    assert client.get('/api/v1/openapi.json').status_code == 200
assert not LifecycleClient('/nonexistent/lifecycle.sock').get_status()[0]
assert generate_openapi_spec()['openapi'] == '3.0.3'
"""
            result = subprocess.run(
                [sys.executable, "-I", "-c", probe, str(installed)],
                cwd=root,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_generated_artifacts_unchanged(self):
        """The extracted generator reproduces the committed public wire artifacts."""
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from ubuntu_tank_protocol.openapi_generator import export_specs; "
                        "import sys; export_specs(sys.argv[1], sys.argv[2])"
                    ),
                    temporary,
                    str(Path(temporary) / "api.ts"),
                ],
                env={
                    **os.environ,
                    "PYTHONPATH": str(WORKSPACE / "src/ubuntu_tank_protocol"),
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for generated, committed in (
                ("openapi_v1.json", "docs/openapi_v1.json"),
                ("api.ts", "web/src/types/api.ts"),
            ):
                self.assertEqual(
                    (Path(temporary) / generated).read_bytes(),
                    (WORKSPACE / committed).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
