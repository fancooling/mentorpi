"""Exercise C2 using real Supervisor, lifecycle sockets and motor-free children.

No Docker, host installation, ROS, serial device or motor access is involved.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TANK = ROOT / "ubuntu_tank"
PACKAGES = [str(p) for p in (TANK / "src").iterdir() if p.is_dir()]
sys.path[:0] = PACKAGES
from ubuntu_tank_supervisor import progress
from ubuntu_tank_supervisor.supervisor_api import call
from ubuntu_tank_web.lifecycle_client import LifecycleClient


def wait_for(predicate, timeout=6):
    """Wait for an observable process outcome, failing on the explicit deadline."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except (OSError, KeyError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError("Process outcome deadline exceeded")


def dead(pid):
    """Treat reaped and zombie processes as stopped, without signalling them."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] == "Z"
    except FileNotFoundError:
        return True


class RuntimeProcesses(unittest.TestCase):
    """Launch the actual Supervisor configuration against real runtime entrypoints."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="c2-")
        self.directory = Path(self.tmp.name)
        (self.directory / "graph-gate").touch()
        self.env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(PACKAGES),
            UBUNTU_TANK_PRIVATE_DIR=str(self.directory),
            UBUNTU_TANK_PREFIX=str(TANK),
            UBUNTU_TANK_PYTHON=sys.executable,
            UBUNTU_TANK_SIMULATION="1",
            C2_GRAPH_GATE=str(self.directory / "graph-gate"),
            UBUNTU_TANK_OPERATOR_SOCKET=str(self.directory / "operator.sock"),
            UBUNTU_TANK_OPERATOR_LOCK=str(self.directory / "authority.lock"),
            UBUNTU_TANK_LIFECYCLE_SOCKET=str(self.directory / "lifecycle.sock"),
            UBUNTU_TANK_CONFIG=str(TANK / "config/controller.yaml"),
            UBUNTU_TANK_WEB_CONFIG=str(TANK / "config/web/web.yaml"),
            ROS_LOG_DIR=str(self.directory / "ros-log"),
            _UBUNTU_TANK_TEST_CHILD_CMD=f"{sys.executable} {TANK / 'tests/fixtures/runtime_graph.py'}",
        )
        self.approval = self.directory / "ready.json"
        self.approval.write_text(
            json.dumps(
                {
                    "token": "test-generation",
                    "release_id": "test-pair",
                    "boot_id": Path("/proc/sys/kernel/random/boot_id")
                    .read_text()
                    .strip(),
                }
            )
        )
        self.env.update(
            UBUNTU_TANK_DEPLOYMENT_DIR=str(self.directory),
            UBUNTU_TANK_DEPLOYMENT_TOKEN="test-generation",
            UBUNTU_TANK_RELEASE_ID="test-pair",
        )
        self.patch = patch.dict(os.environ, {"UBUNTU_TANK_PRIVATE_DIR": self.tmp.name})
        self.patch.start()
        self.output = (self.directory / "output.log").open("w")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "supervisor.supervisord",
                "-c",
                str(ROOT / "docker/ubuntu_tank/supervisord.conf"),
            ],
            env=self.env,
            stdout=self.output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.addCleanup(self.cleanup)
        self.client = LifecycleClient(
            socket_path=self.env["UBUNTU_TANK_LIFECYCLE_SOCKET"]
        )
        wait_for(
            lambda: all(
                progress.fresh(name)
                for name in ("monitor", "operator", "operator_ipc", "lifecycle")
            )
        )

    def cleanup(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        # Reap any deliberately frozen monitor or unexpected survivors.
        for name in ("monitor", "operator", "lifecycle", "controller"):
            record = progress.read_record(name)
            for key in ("pid", "graph_pid"):
                if record.get(key):
                    progress.signal_group(record[key], signal.SIGKILL)
        self.output.close()
        self.patch.stop()
        self.tmp.cleanup()

    def start_controller(self):
        result = self.client.start_controller(timeout_sec=12)
        if not result[0]:
            logs = {p.name: p.read_text()[-4000:] for p in self.directory.glob("*.log")}
            self.fail(f"Start failed: {result}: {logs}")
        return progress.read_record("controller")

    def test_deployment_revocation_blocks_start_and_stops_running_graph(self):
        record = self.start_controller()
        saved = self.approval.read_text()
        self.approval.unlink()
        wait_for(lambda: dead(record["graph_pid"]))
        self.assertFalse(self.client.start_controller(timeout_sec=3)[0])
        self.assertTrue(self.client.stop_controller()[0])
        self.approval.write_text(saved)
        wait_for(lambda: self.client.get_status()[1] == "inactive")
        self.start_controller()

    def test_stopped_boot_start_stop_and_logs(self):
        self.assertEqual(self.client.get_status()[1], "inactive")
        record = self.start_controller()
        info = call("getProcessInfo", "controller")
        self.assertEqual(info["pid"], record["pid"])
        response = self.client.get_logs(limit=200)
        self.assertTrue(response[0])
        result = self.client.stop_controller()
        self.assertTrue(
            result[0],
            (
                result,
                {p.name: p.read_text()[-2500:] for p in self.directory.glob("*.log")},
            ),
        )
        wait_for(lambda record=record: dead(record["graph_pid"]))
        self.assertFalse(progress.read_record("permit"))

    def test_immediate_restart_after_confirmed_stop(self):
        """A completed Stop permits immediate Start after old-group cleanup."""
        for cycle in range(5):
            with self.subTest(cycle=cycle):
                old = self.start_controller()
                self.assertTrue(self.client.stop_controller()[0])
                new = self.start_controller()
                self.assertTrue(new["ready"])
                self.assertNotEqual(new["pid"], old["pid"])
                self.assertTrue(dead(old["graph_pid"]))
                self.assertTrue(self.client.stop_controller()[0])

    def test_stop_cancels_start_waiting_for_heartbeats(self):
        gate = self.directory / "graph-gate"
        gate.unlink()
        result = []
        worker = threading.Thread(
            target=lambda: result.append(self.client.start_controller(timeout_sec=12))
        )
        worker.start()
        wait_for(lambda: progress.read_record("controller").get("graph_pid", 0) > 0)
        record = progress.read_record("controller")
        self.assertTrue(self.client.stop_controller()[0])
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result[0][0])
        gate.touch()
        self.assertTrue(dead(record["graph_pid"]))
        self.assertEqual(self.client.get_status()[1], "inactive")
        wait_for(
            lambda: not progress.read_record("controller") and progress.fresh("monitor")
        )
        self.assertTrue(self.start_controller()["ready"])

    def test_malformed_frames_and_same_uid_sockets(self):
        path = self.env["UBUNTU_TANK_LIFECYCLE_SOCKET"]
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
        for payload in (b"[]\n", b"null\n", b"{broken}\n", b"x" * 65537 + b"\n"):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(path)
                client.sendall(payload)
                response = json.loads(client.recv(4096))
                self.assertFalse(response["success"])
        self.assertEqual(self.client.get_status()[1], "inactive")

    def test_required_process_exit_terminates_runtime(self):
        record = self.start_controller()
        os.kill(progress.read_record("lifecycle")["pid"], signal.SIGKILL)
        wait_for(lambda record=record: dead(record["graph_pid"]))
        wait_for(lambda: self.proc.poll() is not None)

    def test_guard_and_bridge_stale_stop_full_graph(self):
        for role in ("guard", "bridge"):
            with self.subTest(role=role):
                record = self.start_controller()
                pid = int((self.directory / f"{role}.pid").read_text())
                os.kill(pid, signal.SIGSTOP)
                wait_for(lambda record=record: dead(record["graph_pid"]))
                wait_for(lambda: self.client.get_status()[1] == "inactive")
                self.assertTrue(dead(pid))
                self.assertFalse(progress.read_record("permit"))

    def test_safety_monitor_hang_stops_controller(self):
        record = self.start_controller()
        os.kill(progress.read_record("monitor")["pid"], signal.SIGSTOP)
        wait_for(lambda record=record: dead(record["graph_pid"]))
        # Graph exit precedes completion of the runner's bounded cleanup.
        wait_for(lambda: not progress.read_record("permit"))

    def assert_hang_stops_runtime(self, role):
        # Independent runtimes ensure no previous fault masks the next one.
        record = self.start_controller()
        pid = (
            self.proc.pid
            if role == "supervisord"
            else progress.read_record(role)["pid"]
        )
        os.kill(pid, signal.SIGSTOP)
        wait_for(lambda record=record: dead(record["graph_pid"]))
        wait_for(lambda: self.proc.poll() is not None)
        self.assertFalse(progress.read_record("permit"))

    def test_operator_hang(self):
        self.assert_hang_stops_runtime("operator")

    def test_lifecycle_hang(self):
        self.assert_hang_stops_runtime("lifecycle")

    def test_runner_hang(self):
        self.assert_hang_stops_runtime("controller")

    def test_supervisord_hang(self):
        self.assert_hang_stops_runtime("supervisord")

    def test_controller_exit_requires_explicit_restart(self):
        record = self.start_controller()
        os.kill(record["pid"], signal.SIGKILL)
        wait_for(lambda record=record: dead(record["graph_pid"]))
        wait_for(lambda: not progress.read_record("permit"))
        self.assertEqual(self.client.get_status()[1], "inactive")
        wait_for(
            lambda: not progress.read_record("controller") and progress.fresh("monitor")
        )
        self.assertNotEqual(self.start_controller()["pid"], record["pid"])


class RuntimeEpoch(unittest.TestCase):
    """Exercise authority revocation through the real operator safety timer."""

    def test_revocation_invalidates_driving_and_pending_arm(self):
        from ubuntu_tank_operator.agent_node import OperatorAgentNode
        from ubuntu_tank_protocol.enums import OperatorState
        from ubuntu_tank_protocol.ipc_client import OperatorIpcClient

        for driving in (False, True):
            with (
                self.subTest(driving=driving),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {"UBUNTU_TANK_PRIVATE_DIR": directory}),
            ):
                node = OperatorAgentNode(socket_path=directory + "/operator.sock")
                twists = []
                node._publish_twist = lambda x, z, twists=twists: twists.append((x, z))
                try:
                    now = time.monotonic_ns()
                    sm = node.state_machine
                    sm.update_guard_telemetry(False, now)
                    sm.update_battery_telemetry(12.0, now)
                    with OperatorIpcClient(
                        socket_path=directory + "/operator.sock"
                    ) as client:
                        client.acquire("epoch-test")
                        epoch = sm.epoch
                        if driving:
                            self.assertTrue(client.arm(epoch=epoch)[0])
                            challenge = client.request_challenge(epoch)
                            client.submit_intent(
                                challenge["token"], epoch, 1, "forward"
                            )
                            self.assertEqual(sm.state, OperatorState.DRIVING)
                        else:
                            self.assertTrue(
                                sm.arm(
                                    "epoch-test",
                                    epoch,
                                    now,
                                    request_id="pending",
                                )[0]
                            )
                            self.assertEqual(sm.state, OperatorState.ARMING)
                        twists.clear()
                        progress.revoke()
                        node._timer_tick()
                        self.assertEqual(sm.state, OperatorState.NO_OWNER)
                        self.assertIsNone(sm.owner_id)
                        self.assertGreater(sm.epoch, epoch)
                        self.assertTrue(twists)
                        self.assertTrue(all(twist == (0.0, 0.0) for twist in twists))
                        self.assertFalse(
                            sm.confirm_armed(
                                True, True, time.monotonic_ns(), epoch, "pending"
                            )[0]
                        )
                        self.assertFalse(client.arm(epoch=epoch)[0])
                finally:
                    node.destroy_node()


if __name__ == "__main__":
    unittest.main()
