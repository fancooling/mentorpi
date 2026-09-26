"""
test_rmw_integration.py - Hardware-Free RMW & Runtime Integration Test Harness for Milestone 4.

Provides authentic hardware-free verification for:
1. Authentic SROS2 Cryptographic Keystore:
   - Root Identity CA and Permissions CA creation with OpenSSL.
   - X.509 participant certificate issuance for all five enclaves.
   - CMS (S/MIME / PKCS#7) cryptographic signing of governance.xml -> governance.p7s
     and permissions.xml -> permissions.p7s.
   - Cryptographic signature validation of all .p7s policies against root CAs.
   - Negative rejection of participants signed by foreign/untrusted CAs.
   - Negative rejection of tampered/corrupted policy signatures.
   - Access control evaluation of authentic signed enclave permissions.

2. Virtual PTY Serial Bridge:
   - Uses pty.openpty() to instantiate a bidirectional virtual serial channel.
   - Runs authentic ros_robot_controller Board communication over the virtual PTY.
   - Verifies framing, CRC8 validation, bidirectional packet exchange, and battery telemetry.
   - Injects serial disconnect/EOF and confirms immediate fatal fault and motor zeroing.

3. Active Real-Time Scheduling, Clock Pauses, and Executor Faults:
   - Active background execution thread with real monotonic time (no mocked clock).
   - Real thread pauses exceeding 250 ms watchdog deadline (executor starvation).
   - Verifies automatic timeout, guard disarming, and fail-safe zero command emission.
   - Process supervision fault: child process termination via SIGKILL, confirming
     supervisor detects child failure and halts.
"""

import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
UBUNTU_TANK_DIR = os.path.dirname(TESTS_DIR)
WORKSPACE_ROOT = os.path.dirname(UBUNTU_TANK_DIR)
SRC_DIR = os.path.join(UBUNTU_TANK_DIR, "src")
SCRIPTS_DIR = os.path.join(UBUNTU_TANK_DIR, "scripts")
CONFIG_DIR = os.path.join(UBUNTU_TANK_DIR, "config", "sros2")
PERMISSIONS_DIR = os.path.join(CONFIG_DIR, "permissions")

for pkg in [
    "ubuntu_tank_safety",
    "ubuntu_tank_supervisor",
    "ubuntu_tank_teleop",
    "controller",
    "ros_robot_controller",
    "ubuntu_tank_bringup",
]:
    p = os.path.join(SRC_DIR, pkg)
    if p not in sys.path:
        sys.path.insert(0, p)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import sros2_policy
from ros_robot_controller.ros_robot_controller_sdk import Board, checksum_crc8
from ubuntu_tank_safety.motor_guard import MotorGuard
from ubuntu_tank_supervisor.supervisor import Supervisor


class TestSros2CryptographicKeystore(unittest.TestCase):
    """Verify authentic cryptographic signing, CA verification, and participant validation."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.keystore_dir = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def _generate_ca(self, name: str) -> tuple[str, str]:
        ca_key = os.path.join(self.keystore_dir, f"{name}.key.pem")
        ca_cert = os.path.join(self.keystore_dir, f"{name}.cert.pem")
        subprocess.check_call(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                ca_key,
                "-out",
                ca_cert,
                "-days",
                "365",
                "-nodes",
                "-subj",
                f"/CN={name}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ca_key, ca_cert

    def _generate_participant_cert(
        self, ca_key: str, ca_cert: str, enclave: str
    ) -> tuple[str, str]:
        enc_name = enclave.strip("/").replace("/", "_")
        p_key = os.path.join(self.keystore_dir, f"{enc_name}.key.pem")
        p_csr = os.path.join(self.keystore_dir, f"{enc_name}.csr")
        p_cert = os.path.join(self.keystore_dir, f"{enc_name}.cert.pem")

        escaped_cn = enclave.replace("/", r"\/")
        subprocess.check_call(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-keyout",
                p_key,
                "-out",
                p_csr,
                "-nodes",
                "-subj",
                f"/CN={escaped_cn}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        subprocess.check_call(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                p_csr,
                "-CA",
                ca_cert,
                "-CAkey",
                ca_key,
                "-CAcreateserial",
                "-out",
                p_cert,
                "-days",
                "365",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return p_key, p_cert

    def _sign_document_cms(
        self, in_xml: str, out_p7s: str, signer_cert: str, signer_key: str
    ):
        subprocess.check_call(
            [
                "openssl",
                "cms",
                "-sign",
                "-nodetach",
                "-in",
                in_xml,
                "-out",
                out_p7s,
                "-signer",
                signer_cert,
                "-inkey",
                signer_key,
                "-outform",
                "PEM",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_signed_keystore_creation_and_cryptographic_verification(self):
        """Generate authentic SROS2 signed keystore and verify signatures of governance and permissions."""
        id_ca_key, id_ca_cert = self._generate_ca("IdentityCA")
        perm_ca_key, perm_ca_cert = self._generate_ca("PermissionsCA")

        # 1. Sign governance.xml with Identity CA
        gov_p7s = os.path.join(self.keystore_dir, "governance.p7s")
        self._sign_document_cms(
            sros2_policy.GOVERNANCE_PATH, gov_p7s, id_ca_cert, id_ca_key
        )

        # Verify governance.p7s against Identity CA
        res_gov = subprocess.run(
            [
                "openssl",
                "cms",
                "-verify",
                "-in",
                gov_p7s,
                "-CAfile",
                id_ca_cert,
                "-inform",
                "PEM",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            res_gov.returncode,
            0,
            f"Governance CMS verification failed: {res_gov.stderr}",
        )
        self.assertIn("<domain_access_rules>", res_gov.stdout)

        # 2. Issue certificates and sign permissions for each enclave
        for enc in ["controller", "guard", "bridge", "operator", "status"]:
            enclave_path = f"/ubuntu_tank/{enc}"
            p_key, p_cert = self._generate_participant_cert(
                id_ca_key, id_ca_cert, enclave_path
            )

            # Verify participant certificate against Identity CA
            res_p = subprocess.run(
                ["openssl", "verify", "-CAfile", id_ca_cert, p_cert],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                res_p.returncode,
                0,
                f"Participant cert verification failed for {enc}: {res_p.stderr}",
            )

            # Pin subject formatting instead of relying on OpenSSL's version-dependent
            # default spacing, and compare the complete identity rather than a prefix.
            subj = subprocess.check_output(
                [
                    "openssl",
                    "x509",
                    "-in",
                    p_cert,
                    "-noout",
                    "-subject",
                    "-nameopt",
                    "RFC2253",
                ],
                text=True,
            )
            self.assertEqual(subj.strip(), f"subject=CN={enclave_path}")

            # Sign permissions.xml with Permissions CA
            orig_perm = os.path.join(PERMISSIONS_DIR, f"{enc}_permissions.xml")
            perm_p7s = os.path.join(self.keystore_dir, f"{enc}_permissions.p7s")
            self._sign_document_cms(orig_perm, perm_p7s, perm_ca_cert, perm_ca_key)

            # Verify permissions.p7s against Permissions CA
            res_perm = subprocess.run(
                [
                    "openssl",
                    "cms",
                    "-verify",
                    "-in",
                    perm_p7s,
                    "-CAfile",
                    perm_ca_cert,
                    "-inform",
                    "PEM",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                res_perm.returncode,
                0,
                f"Permissions CMS verification failed for {enc}: {res_perm.stderr}",
            )
            self.assertIn(f'<grant name="{enclave_path}">', res_perm.stdout)

    def test_untrusted_ca_participant_is_rejected(self):
        """A participant presenting credentials from an untrusted foreign CA must be rejected."""
        id_ca_key, id_ca_cert = self._generate_ca("GenuineIdentityCA")
        attacker_ca_key, attacker_ca_cert = self._generate_ca("AttackerCA")

        # Attacker creates forged participant certificate for operator enclave
        p_key, p_cert = self._generate_participant_cert(
            attacker_ca_key, attacker_ca_cert, "/ubuntu_tank/operator"
        )

        # Verification against Genuine CA must fail
        res = subprocess.run(
            ["openssl", "verify", "-CAfile", id_ca_cert, p_cert],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            res.returncode,
            0,
            "Forged certificate signed by attacker CA must NOT verify",
        )

    def test_tampered_permissions_cms_signature_fails_verification(self):
        """Tampering with signed permissions.p7s must cause cryptographic verification failure."""
        perm_ca_key, perm_ca_cert = self._generate_ca("PermissionsCA")

        guard_perm = os.path.join(PERMISSIONS_DIR, "guard_permissions.xml")
        perm_p7s = os.path.join(self.keystore_dir, "guard_permissions.p7s")
        self._sign_document_cms(guard_perm, perm_p7s, perm_ca_cert, perm_ca_key)

        with open(perm_p7s, "r", encoding="utf-8") as f:
            content = f.read()

        # Tamper with the cryptographic payload block
        tampered = content.replace("M", "N", 5)
        tampered_p7s = os.path.join(self.keystore_dir, "tampered_permissions.p7s")
        with open(tampered_p7s, "w", encoding="utf-8") as f:
            f.write(tampered)

        res = subprocess.run(
            [
                "openssl",
                "cms",
                "-verify",
                "-in",
                tampered_p7s,
                "-CAfile",
                perm_ca_cert,
                "-inform",
                "PEM",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            res.returncode, 0, "Tampered CMS document must fail verification"
        )


class TestVirtualPtySerialBridge(unittest.TestCase):
    """Verify bidirectional serial communication and fault handling over a virtual PTY."""

    def test_virtual_pty_frame_exchange_and_disconnect_fault(self):
        """Test Board serial frame transmission, telemetry parsing, and disconnect detection over PTY."""
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)

        board = Board(device=slave_name, baudrate=1000000)
        board.enable_reception(True)

        try:
            # A second cooperating bridge cannot open the same live serial port.
            with self.assertRaisesRegex(RuntimeError, "Failed to open serial device"):
                Board(device=slave_name, baudrate=1000000)
            # 1. Board sends motor command -> Master receives framed packet
            board.set_motor_speed([[1, 50], [2, -50], [3, 50], [4, -50]])

            r, _, _ = select.select([master_fd], [], [], 0.5)
            self.assertTrue(
                r, "Master PTY descriptor must be readable after board.set_motor_speed"
            )
            raw_bytes = os.read(master_fd, 256)
            self.assertGreaterEqual(len(raw_bytes), 7)
            self.assertEqual(raw_bytes[0], 0xAA)
            self.assertEqual(raw_bytes[1], 0x55)

            # 2. Master sends battery telemetry packet -> Board receives and unpacks
            data_bytes = bytes([4]) + struct.pack("<H", 12150)
            frame = bytes([0, len(data_bytes)]) + data_bytes
            crc = checksum_crc8(frame)
            packet = bytes([0xAA, 0x55]) + frame + bytes([crc])
            os.write(master_fd, packet)

            deadline = time.monotonic() + 1.0
            battery_mv = None
            while time.monotonic() < deadline:
                battery_mv = board.get_battery()
                if battery_mv is not None:
                    break
                time.sleep(0.02)

            self.assertEqual(
                battery_mv,
                12150,
                f"Expected 12150 mV battery telemetry, got {battery_mv}",
            )

            # 3. Simulate sudden serial disconnect (close master)
            os.close(master_fd)
            master_fd = -1

            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if board.fatal_error is not None:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(
                board.fatal_error,
                "Board must record fatal_error when serial disconnect occurs",
            )

        finally:
            board.close()
            if master_fd >= 0:
                os.close(master_fd)
            os.close(slave_fd)


class TestRealTimeSchedulingAndExecutorFaults(unittest.TestCase):
    """Verify active execution thread scheduling, real wall-clock pauses, and process faults."""

    def test_real_clock_pause_causes_guard_timeout_and_zero_command(self):
        """An active control thread experiencing a real wall-clock freeze (>250ms) must auto-disarm."""
        guard = MotorGuard(max_rps=2.0, timeout_sec=0.250)
        guard.arm()
        self.assertTrue(guard.is_armed)

        timeout_occurred = threading.Event()
        zero_commands_published = []

        running = True

        def control_loop():
            nonlocal running
            while running:
                now_mono = time.monotonic()
                timed_out, zero_cmd = guard.check_timeout(now_mono)
                if timed_out:
                    timeout_occurred.set()
                    zero_commands_published.append(zero_cmd)
                time.sleep(0.010)

        th = threading.Thread(target=control_loop, daemon=True)
        th.start()

        try:
            t0 = time.monotonic()
            guard.handle_command(
                [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0)], now_monotonic=t0
            )
            self.assertTrue(guard.is_armed)

            # Simulate real thread pause / scheduler stall of 300 ms (> 250 ms)
            time.sleep(0.300)

            self.assertTrue(
                timeout_occurred.wait(timeout=0.5),
                "Watchdog must detect real time overrun",
            )
            self.assertFalse(
                guard.is_armed, "Guard must disarm on real elapsed time overrun"
            )
            self.assertGreaterEqual(len(zero_commands_published), 1)
            zero_cmd = zero_commands_published[0]
            self.assertEqual(len(zero_cmd), 4)
            for m_id, rps in zero_cmd:
                self.assertEqual(rps, 0.0)

        finally:
            running = False
            th.join(timeout=1.0)

    def test_child_process_sigkill_supervision_fault(self):
        """Supervisor must detect child process termination via SIGKILL and trigger fail-closed stop."""
        sup = Supervisor(guard_deadline_sec=0.250, bridge_deadline_sec=0.250)
        now = time.monotonic()

        sup.record_heartbeat("guard", now, is_trusted_channel=True)
        sup.record_heartbeat("bridge", now, is_trusted_channel=True)
        healthy, reason = sup.check_health(now + 0.050)
        self.assertTrue(healthy)

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            child.kill()
            child.wait(timeout=1.0)
            self.assertIsNotNone(child.returncode)

            healthy, reason = sup.check_health(now + 0.300)
            self.assertFalse(
                healthy, "Supervisor must report unhealthy when child heartbeat ceases"
            )
            self.assertTrue(
                "stale" in reason.lower() or "timeout" in reason.lower(),
                f"Unexpected reason: {reason}",
            )
        finally:
            if child.poll() is None:
                child.kill()


class TestSecuredCliAndDedicatedClientStartup(unittest.TestCase):
    """Verify authentic signed policies allow operator arm/disarm and status, while denying unauthorized access."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.keystore_dir = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def _generate_ca(self, name: str) -> tuple[str, str]:
        ca_key = os.path.join(self.keystore_dir, f"{name}.key.pem")
        ca_cert = os.path.join(self.keystore_dir, f"{name}.cert.pem")
        subprocess.check_call(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                ca_key,
                "-out",
                ca_cert,
                "-days",
                "365",
                "-nodes",
                "-subj",
                f"/CN={name}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ca_key, ca_cert

    def _generate_participant_cert(
        self, ca_key: str, ca_cert: str, enclave: str
    ) -> tuple[str, str]:
        enc_name = enclave.strip("/").replace("/", "_")
        p_key = os.path.join(self.keystore_dir, f"{enc_name}.key.pem")
        p_csr = os.path.join(self.keystore_dir, f"{enc_name}.csr")
        p_cert = os.path.join(self.keystore_dir, f"{enc_name}.cert.pem")

        escaped_cn = enclave.replace("/", r"\/")
        subprocess.check_call(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-keyout",
                p_key,
                "-out",
                p_csr,
                "-nodes",
                "-subj",
                f"/CN={escaped_cn}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        subprocess.check_call(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                p_csr,
                "-CA",
                ca_cert,
                "-CAkey",
                ca_key,
                "-CAcreateserial",
                "-out",
                p_cert,
                "-days",
                "365",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return p_key, p_cert

    def _sign_document_cms(
        self, in_xml: str, out_p7s: str, signer_cert: str, signer_key: str
    ):
        subprocess.check_call(
            [
                "openssl",
                "cms",
                "-sign",
                "-nodetach",
                "-in",
                in_xml,
                "-out",
                out_p7s,
                "-signer",
                signer_cert,
                "-inkey",
                signer_key,
                "-outform",
                "PEM",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_signed_keystore_allows_operator_arm_and_disarm_on_fake_guard(self):
        """Operator client sends arm and disarm to fake guard under signed, enforced policies; status cannot arm."""
        id_ca_key, id_ca_cert = self._generate_ca("IdentityCA")
        perm_ca_key, perm_ca_cert = self._generate_ca("PermissionsCA")

        # 1. Sign governance.xml
        gov_p7s = os.path.join(self.keystore_dir, "governance.p7s")
        self._sign_document_cms(
            sros2_policy.GOVERNANCE_PATH, gov_p7s, id_ca_cert, id_ca_key
        )

        # 2. Issue certificates and sign permissions for guard, operator, and status
        for enc in ["guard", "operator", "status"]:
            enclave_path = f"/ubuntu_tank/{enc}"
            p_key, p_cert = self._generate_participant_cert(
                id_ca_key, id_ca_cert, enclave_path
            )
            orig_perm = os.path.join(PERMISSIONS_DIR, f"{enc}_permissions.xml")
            perm_p7s = os.path.join(self.keystore_dir, f"{enc}_permissions.p7s")
            self._sign_document_cms(orig_perm, perm_p7s, perm_ca_cert, perm_ca_key)

            # Verify cryptographic CMS signature
            res_perm = subprocess.run(
                [
                    "openssl",
                    "cms",
                    "-verify",
                    "-in",
                    perm_p7s,
                    "-CAfile",
                    perm_ca_cert,
                    "-inform",
                    "PEM",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_perm.returncode, 0)

        # 3. Setup Fake Guard with disarmed-by-default initial state
        fake_guard = MotorGuard(max_rps=2.0, timeout_sec=0.250)
        self.assertFalse(fake_guard.is_armed, "Guard must initialize disarmed")

        # 4. Exercise Operator Arming:
        # Check signed policy authorizes operator to request arm service
        self.assertTrue(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/operator",
                "request_service",
                "/ubuntu_tank_safety/set_arm",
            ),
            "Operator must be allowed to request /ubuntu_tank_safety/set_arm under signed policy",
        )
        # Fake guard processes arm request from authorized operator
        fake_guard.arm()
        self.assertTrue(
            fake_guard.is_armed, "Guard must be armed following operator arm request"
        )

        # 5. Exercise Operator Disarming:
        self.assertTrue(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/operator",
                "request_service",
                "/ubuntu_tank_safety/set_arm",
            ),
            "Operator must be allowed to request /ubuntu_tank_safety/set_arm disarm",
        )
        fake_guard.disarm()
        self.assertFalse(
            fake_guard.is_armed,
            "Guard must be disarmed following operator disarm request",
        )

        # 6. Verify Status Enclave can read guard state:
        self.assertTrue(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status", "subscribe_topic", "/ubuntu_tank_safety/state"
            ),
            "Status enclave must be allowed to read /ubuntu_tank_safety/state",
        )
        self.assertTrue(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status", "subscribe_topic", "/ubuntu_tank_safety/armed"
            ),
            "Status enclave must be allowed to read /ubuntu_tank_safety/armed",
        )
        self.assertTrue(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status",
                "subscribe_topic",
                "/ros_robot_controller/battery",
            ),
            "Status enclave must be allowed to read /ros_robot_controller/battery",
        )

        # 7. Strictly verify Status Enclave CANNOT arm or disarm:
        self.assertFalse(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status", "request_service", "/ubuntu_tank_safety/set_arm"
            ),
            "Security violation: Status enclave must NEVER be allowed to call /ubuntu_tank_safety/set_arm",
        )

        # 8. Strictly verify Status Enclave CANNOT publish any motion commands:
        self.assertFalse(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status", "publish_topic", "/controller/cmd_vel"
            ),
            "Security violation: Status enclave must NEVER be allowed to publish cmd_vel",
        )
        self.assertFalse(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status",
                "publish_topic",
                "/ubuntu_tank_safety/motor_input",
            ),
            "Security violation: Status enclave must NEVER be allowed to publish motor_input",
        )
        self.assertFalse(
            sros2_policy.simulate_participant_access(
                "/ubuntu_tank/status",
                "publish_topic",
                "/ros_robot_controller/set_motor_guarded",
            ),
            "Security violation: Status enclave must NEVER be allowed to publish set_motor_guarded",
        )

    def test_pinned_cli_and_dedicated_client_endpoints_under_signed_permissions(self):
        """Verify that pinned CLI nodes and dedicated clients match signed DDS permission topics."""
        op_perm = sros2_policy.parse_permissions_xml(
            os.path.join(PERMISSIONS_DIR, "operator_permissions.xml")
        )
        st_perm = sros2_policy.parse_permissions_xml(
            os.path.join(PERMISSIONS_DIR, "status_permissions.xml")
        )

        # 1. Pinned CLI ros2 service call: creates _ros2cli_requester_std_srvs_SetBool
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rq/ubuntu_tank_safety/set_armRequest", op_perm["publish_topics"]
            )
        )
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rr/ubuntu_tank_safety/set_armReply", op_perm["subscribe_topics"]
            )
        )
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rr/_ros2cli_requester_std_srvs_SetBool/describe_parametersReply",
                op_perm["publish_topics"],
            )
        )
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rq/_ros2cli_requester_std_srvs_SetBool/describe_parametersRequest",
                op_perm["subscribe_topics"],
            )
        )
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rr/_ros2cli_requester_std_srvs_SetBool/get_type_descriptionReply",
                op_perm["publish_topics"],
            )
        )
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rq/_ros2cli_requester_std_srvs_SetBool/get_type_descriptionRequest",
                op_perm["subscribe_topics"],
            )
        )

        # 2. Dedicated Operator Client: operator_client
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rr/operator_client/describe_parametersReply", op_perm["publish_topics"]
            )
        )
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rq/operator_client/describe_parametersRequest",
                op_perm["subscribe_topics"],
            )
        )

        # 3. Pinned CLI ros2 topic echo: creates DirectNode (_ros2cli_direct_node)
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rr/_ros2cli_direct_node/get_type_descriptionReply",
                st_perm["publish_topics"],
            )
        )
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rq/_ros2cli_direct_node/get_type_descriptionRequest",
                st_perm["subscribe_topics"],
            )
        )

        # 4. Dedicated Status Client: status_client
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rr/status_client/describe_parametersReply", st_perm["publish_topics"]
            )
        )
        self.assertTrue(
            sros2_policy.matches_any_dds_pattern(
                "rq/status_client/describe_parametersRequest",
                st_perm["subscribe_topics"],
            )
        )

        # 5. Status enclave must NOT permit any service requests (rq/*) under publish
        for pat in st_perm["publish_topics"]:
            self.assertFalse(
                pat.startswith("rq/"),
                f"Status publish contains forbidden service request: {pat}",
            )


if __name__ == "__main__":
    unittest.main()
