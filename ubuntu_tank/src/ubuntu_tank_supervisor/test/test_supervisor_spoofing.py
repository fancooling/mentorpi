"""
Integration tests for Supervisor credential PID mismatch rejection and trusted pipe integration.

Verifies:
1. Senders with mismatched PIDs are rejected and cannot refresh deadlines to trigger systemd WATCHDOG pings.
2. Unconfigured expected PIDs fail-closed and reject arbitrary socket heartbeats.
3. Inherited isolated kernel pipes successfully refresh health deadlines and emit WATCHDOG pings.
"""

import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


class TestSupervisorSpoofingDefense(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.notify_sock_path = os.path.join(self.temp_dir.name, "notify.sock")
        self.guard_sock_path = os.path.join(self.temp_dir.name, "guard.sock")
        self.bridge_sock_path = os.path.join(self.temp_dir.name, "bridge.sock")

        self.notify_server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify_server.bind(self.notify_sock_path)
        self.notify_server.setblocking(False)

    def tearDown(self):
        try:
            self.notify_server.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _recv_notify(self, timeout_sec=0.5):
        r, _, _ = select.select([self.notify_server], [], [], timeout_sec)
        if r:
            data, _ = self.notify_server.recvfrom(256)
            return data.decode("utf-8").strip()
        return None

    def test_same_uid_attacker_spoof_rejected(self):
        """
        Verify that an attacker process sharing the service UID but with a mismatched PID
        CANNOT refresh child deadlines and CANNOT cause the supervisor to emit WATCHDOG=1.
        """
        # Configure supervisor expecting fictitious child PIDs
        pkg_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{pkg_dir}:{env.get('PYTHONPATH', '')}"
        env["NOTIFY_SOCKET"] = self.notify_sock_path
        env["UBUNTU_TANK_GUARD_SOCK"] = self.guard_sock_path
        env["UBUNTU_TANK_BRIDGE_SOCK"] = self.bridge_sock_path
        env["UBUNTU_TANK_GUARD_DEADLINE"] = "0.5"
        env["UBUNTU_TANK_BRIDGE_DEADLINE"] = "0.5"
        env["UBUNTU_TANK_GUARD_PID"] = "999991"
        env["UBUNTU_TANK_BRIDGE_PID"] = "999992"

        proc = subprocess.Popen(
            [sys.executable, "-m", "ubuntu_tank_supervisor.supervisor_node"],
            env=env,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        try:
            # 1. Wait for READY=1
            ready_msg = self._recv_notify(timeout_sec=2.0)
            self.assertEqual(ready_msg, "READY=1")

            # Wait briefly for unix sockets to be created
            for _ in range(20):
                if os.path.exists(self.guard_sock_path) and os.path.exists(
                    self.bridge_sock_path
                ):
                    break
                time.sleep(0.05)

            self.assertTrue(os.path.exists(self.guard_sock_path))
            self.assertTrue(os.path.exists(self.bridge_sock_path))

            # 2. Attacker (this test process, with its own PID != 999991/999992)
            # sends spoofed heartbeats repeatedly for 0.8 seconds
            start = time.monotonic()
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as attacker_sock:
                while time.monotonic() - start < 0.8:
                    payload = f"{time.monotonic():.6f}\n".encode("utf-8")
                    attacker_sock.sendto(payload, self.guard_sock_path)
                    attacker_sock.sendto(payload, self.bridge_sock_path)
                    time.sleep(0.05)

            # 3. Assert WATCHDOG=1 was NEVER emitted to systemd
            watchdog_msg = self._recv_notify(timeout_sec=0.4)
            self.assertIsNone(
                watchdog_msg,
                f"Vulnerability detected: supervisor accepted spoofed heartbeats and emitted {watchdog_msg}",
            )

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_unconfigured_pid_fails_closed(self):
        """
        Verify that socket heartbeats are rejected if the supervisor was started without
        configured child PIDs (fail-closed behavior).
        """
        pkg_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{pkg_dir}:{env.get('PYTHONPATH', '')}"
        env["NOTIFY_SOCKET"] = self.notify_sock_path
        env["UBUNTU_TANK_GUARD_SOCK"] = self.guard_sock_path
        env["UBUNTU_TANK_BRIDGE_SOCK"] = self.bridge_sock_path
        env["UBUNTU_TANK_GUARD_DEADLINE"] = "0.5"
        env["UBUNTU_TANK_BRIDGE_DEADLINE"] = "0.5"
        env.pop("UBUNTU_TANK_GUARD_PID", None)
        env.pop("UBUNTU_TANK_BRIDGE_PID", None)

        proc = subprocess.Popen(
            [sys.executable, "-m", "ubuntu_tank_supervisor.supervisor_node"],
            env=env,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        try:
            ready_msg = self._recv_notify(timeout_sec=2.0)
            self.assertEqual(ready_msg, "READY=1")

            for _ in range(20):
                if os.path.exists(self.guard_sock_path) and os.path.exists(
                    self.bridge_sock_path
                ):
                    break
                time.sleep(0.05)

            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as attacker_sock:
                for _ in range(5):
                    payload = f"{time.monotonic():.6f}\n".encode("utf-8")
                    attacker_sock.sendto(payload, self.guard_sock_path)
                    attacker_sock.sendto(payload, self.bridge_sock_path)
                    time.sleep(0.05)

            watchdog_msg = self._recv_notify(timeout_sec=0.4)
            self.assertIsNone(
                watchdog_msg,
                "Unconfigured supervisor must fail-closed and reject unauthenticated socket heartbeats",
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_inherited_pipes_trusted_and_gating(self):
        """
        Verify that isolated inherited kernel pipes successfully deliver heartbeats
        and satisfy the AND-gated watchdog requirement.
        """
        r_guard, w_guard = os.pipe()
        r_bridge, w_bridge = os.pipe()

        pkg_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{pkg_dir}:{env.get('PYTHONPATH', '')}"
        env["NOTIFY_SOCKET"] = self.notify_sock_path
        env["UBUNTU_TANK_GUARD_PIPE_FD"] = str(r_guard)
        env["UBUNTU_TANK_BRIDGE_PIPE_FD"] = str(r_bridge)
        env["UBUNTU_TANK_GUARD_DEADLINE"] = "0.6"
        env["UBUNTU_TANK_BRIDGE_DEADLINE"] = "0.6"

        proc = subprocess.Popen(
            [sys.executable, "-m", "ubuntu_tank_supervisor.supervisor_node"],
            env=env,
            pass_fds=(r_guard, r_bridge),
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        try:
            ready_msg = self._recv_notify(timeout_sec=2.0)
            self.assertEqual(ready_msg, "READY=1")

            # Write heartbeats to BOTH pipes
            for _ in range(3):
                payload = f"{time.monotonic():.6f}\n".encode("utf-8")
                os.write(w_guard, payload)
                os.write(w_bridge, payload)
                time.sleep(0.1)

            # Expect WATCHDOG=1
            watchdog_msg = self._recv_notify(timeout_sec=0.6)
            self.assertEqual(watchdog_msg, "WATCHDOG=1")

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            os.close(r_guard)
            os.close(w_guard)
            os.close(r_bridge)
            os.close(w_bridge)


if __name__ == "__main__":
    unittest.main()
