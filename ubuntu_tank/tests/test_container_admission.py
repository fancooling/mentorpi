"""Exercise deployment admission through operator state transitions, without hardware."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(p) for p in (ROOT / "ubuntu_tank/src").iterdir() if p.is_dir()]
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_protocol.deployment import admitted
from ubuntu_tank_protocol.enums import OperatorState, WebControlErrorCode
from ubuntu_tank_protocol.schemas import TelemetrySnapshot


class Admission(unittest.TestCase):
    """A missing or stale host approval must cancel pending and active authority."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ready.json"
        self.approval = {
            "release_id": "pair-a",
            "token": "generation-a",
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
        env = patch.dict(
            os.environ,
            UBUNTU_TANK_DEPLOYMENT_DIR=self.directory.name,
            UBUNTU_TANK_DEPLOYMENT_TOKEN="generation-a",
            UBUNTU_TANK_RELEASE_ID="pair-a",
        )
        env.start()
        self.addCleanup(env.stop)
        self.path.write_text(json.dumps(self.approval))
        self.now = time.monotonic_ns()
        self.machine = OperatorStateMachine()
        self.machine.telemetry = TelemetrySnapshot(
            battery_voltage=12.0,
            battery_monotonic_ns=self.now,
            guard_armed=False,
            guard_monotonic_ns=self.now,
            odom_monotonic_ns=self.now,
            delivery_monotonic_ns=self.now,
        )
        result = self.machine.acquire("operator", self.now)
        self.assertTrue(result[0], result)
        self.epoch = self.machine.epoch

    def test_wrong_pair_generation_boot_and_missing_approval_reject_arm(self):
        for field in self.approval:
            with self.subTest(field=field):
                self.path.write_text(
                    json.dumps(dict(self.approval, **{field: "wrong"}))
                )
                self.assertFalse(admitted())
                result = self.machine.arm("operator", self.epoch, self.now, "arm")
                self.assertEqual(result[1], WebControlErrorCode.DEPLOYMENT_BUSY)
        self.path.unlink()
        self.assertFalse(admitted())
        self.machine.stop(self.now)  # Stop remains available while admission is absent.

    def test_gate_revocation_cancels_pending_arm_and_late_success(self):
        self.assertTrue(self.machine.arm("operator", self.epoch, self.now, "arm")[0])
        self.path.unlink()
        result = self.machine.confirm_armed(True, True, self.now + 1, self.epoch, "arm")
        self.assertFalse(result[0])
        self.assertTrue(self.machine.compensating_disarm_required)
        self.assertEqual(self.machine.get_velocity_command(), (0.0, 0.0))

    def test_gate_revocation_releases_authority_and_requires_fresh_ownership(self):
        self.assertTrue(self.machine.arm("operator", self.epoch, self.now, "arm")[0])
        self.assertTrue(
            self.machine.confirm_armed(True, True, self.now + 1, self.epoch, "arm")[0]
        )
        self.path.unlink()
        self.assertFalse(self.machine.check_deadlines(self.now + 2)[0])
        self.assertEqual(self.machine.state, OperatorState.NO_OWNER)
        self.assertIsNone(self.machine.owner_id)
        self.assertNotEqual(self.machine.epoch, self.epoch)
        self.path.write_text(json.dumps(self.approval))
        self.assertFalse(
            self.machine.arm("operator", self.epoch, self.now + 3, "old")[0]
        )
        self.assertEqual(self.machine.get_velocity_command(), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
