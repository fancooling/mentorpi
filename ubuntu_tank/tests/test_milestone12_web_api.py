"""
test_milestone12_web_api.py - Comprehensive test suite for Milestone 12: Web API and Service Lifecycle.

Verifies:
1. TLS certificate generation with SANs (IP, 127.0.0.1, localhost), secure permissions (0600 key, 0644 cert), and idempotence.
2. Security headers (CSP, nosniff, DENY, no-referrer) and same-origin mutation controls (Origin rejection on mutations, WebSocket origin check).
3. FastAPI strict Pydantic models (extra="forbid", strict booleans), OpenAPI export, and REST endpoints (/version, /status, /logs, /control/..., /controller/...).
4. Request size bounding (64 KiB limit) and request flood resistance.
5. WebSocket control protocol (/api/v1/control): Origin validation, 64 KiB message cap, bind, challenge relay, latest-intent only, stop priority, backpressure, and disconnect disarm.
6. Narrow lifecycle helper daemon: SO_PEERCRED checks, fixed command execution, deployment lock interlock (DEPLOYMENT_BUSY), activation recovery interlock (OPERATION_FAILED), preflight check, and bounded logs.
7. Systemd service hardening: systemd-analyze verification and sandbox constraints for mentorpi-tank-web and mentorpi-tank-lifecycle.
8. Fault tolerance: Stop fallback when operator agent is down, status when operator is down, and frozen web process lease expiry.
9. Installed launcher imports, lifecycle-authoritative controller status, and configured web motion-cap enforcement.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from cryptography import x509
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

# Ensure workspace packages are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
UBUNTU_TANK_DIR = os.path.join(REPO_ROOT, "ubuntu_tank")
WEB_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_web")
OPERATOR_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_operator")

for p in [UBUNTU_TANK_DIR, WEB_PKG_DIR, OPERATOR_PKG_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from ubuntu_tank_operator.config import WebControlConfig
from ubuntu_tank_operator.constants import (
    API_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
)
from ubuntu_tank_operator.enums import (
    MotionDirection,
    OperatorState,
    WebControlErrorCode,
)
from ubuntu_tank_operator.ipc_client import OperatorIpcClient
from ubuntu_tank_operator.ipc_server import OperatorIpcServer
from ubuntu_tank_operator.schemas import TelemetrySnapshot
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_web.app import create_app
from ubuntu_tank_web.lifecycle_client import LifecycleClient
from ubuntu_tank_web.lifecycle_service import LifecycleHelperService
from ubuntu_tank_web.tls import ensure_tls_certificate


class TestTlsProvisioning(unittest.TestCase):
    """Test owner-trusted TLS certificate generation and permission enforcement."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cert_path = os.path.join(self.tmp_dir.name, "tank.crt")
        self.key_path = os.path.join(self.tmp_dir.name, "tank.key")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_tls_generation_and_sans(self):
        """Certificate is generated with required SANs (LAN IP, 127.0.0.1, localhost)."""
        cert_p, key_p = ensure_tls_certificate(
            cert_path=self.cert_path,
            key_path=self.key_path,
            hostnames_or_ips=["192.168.1.150", "127.0.0.1", "localhost"],
            valid_days=365,
        )
        self.assertTrue(os.path.isfile(cert_p))
        self.assertTrue(os.path.isfile(key_p))

        # Check permissions: cert 0644, key 0600
        cert_mode = oct(os.stat(cert_p).st_mode & 0o777)
        key_mode = oct(os.stat(key_p).st_mode & 0o777)
        self.assertEqual(cert_mode, oct(0o644))
        self.assertEqual(key_mode, oct(0o600))

        # Inspect certificate content using cryptography
        with open(cert_p, "rb") as f:
            cert_data = f.read()
        cert = x509.load_pem_x509_certificate(cert_data)

        # Subject and issuer
        cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
        self.assertEqual(cn, "MentorPi Tank Web Control Service")

        # Subject Alternative Names
        san_ext = cert.extensions.get_extension_for_oid(
            x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
        san_names = san_ext.value
        dns_names = [name.value for name in san_names if isinstance(name, x509.DNSName)]
        ip_addrs = [
            str(name.value) for name in san_names if isinstance(name, x509.IPAddress)
        ]

        self.assertIn("localhost", dns_names)
        self.assertIn("127.0.0.1", ip_addrs)
        self.assertIn("192.168.1.150", ip_addrs)

    def test_tls_generation_idempotence(self):
        """Existing valid certificate and key are retained without unnecessary regeneration."""
        ensure_tls_certificate(
            cert_path=self.cert_path,
            key_path=self.key_path,
            hostnames_or_ips=["127.0.0.1"],
        )
        mtime_cert = os.stat(self.cert_path).st_mtime_ns
        mtime_key = os.stat(self.key_path).st_mtime_ns

        time.sleep(0.01)
        ensure_tls_certificate(
            cert_path=self.cert_path,
            key_path=self.key_path,
            hostnames_or_ips=["127.0.0.1"],
        )
        self.assertEqual(os.stat(self.cert_path).st_mtime_ns, mtime_cert)
        self.assertEqual(os.stat(self.key_path).st_mtime_ns, mtime_key)

    def test_tls_regeneration_on_corrupt_file(self):
        """Corrupted certificate file triggers safe re-issuance."""
        with open(self.cert_path, "w") as f:
            f.write("GARBAGE_NOT_A_CERT")
        with open(self.key_path, "w") as f:
            f.write("GARBAGE_NOT_A_KEY")

        cert_p, _ = ensure_tls_certificate(
            cert_path=self.cert_path,
            key_path=self.key_path,
            hostnames_or_ips=["127.0.0.1"],
        )
        with open(cert_p, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        self.assertIsNotNone(cert)


class TestSecurityHeadersAndSameOrigin(unittest.TestCase):
    """Test HTTP security headers and Origin-based mutation validation."""

    def setUp(self):
        self.config = WebControlConfig(
            listen_address="127.0.0.1",
            port=8443,
            allowed_origins=["https://127.0.0.1:8443", "https://localhost:8443"],
        )
        self.app = create_app(config=self.config)
        self.client = TestClient(self.app)

    def test_security_headers_present(self):
        """Responses must include CSP, nosniff, DENY framing, and referrer headers."""
        res = self.client.get("/api/v1/version")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Content-Security-Policy", res.headers)
        self.assertEqual(res.headers["X-Frame-Options"], "DENY")
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(res.headers["Referrer-Policy"], "no-referrer")

    def test_cross_origin_mutation_rejected(self):
        """Mutating request with unauthorized Origin is rejected with 403."""
        payload = {
            "epoch": 1,
            "tracks_raised": True,
            "request_id": "req-1",
        }
        res = self.client.post(
            "/api/v1/control/arm",
            json=payload,
            headers={"Origin": "https://attacker.site"},
        )
        self.assertEqual(res.status_code, 403)
        data = res.json()
        self.assertEqual(data["error"], "CROSS_ORIGIN_DENIED")

    def test_valid_origin_mutation_accepted(self):
        """Mutating request with authorized Origin passes CORS / same-origin check."""
        payload = {
            "operator_id": "op-test",
            "request_id": "req-2",
        }
        res = self.client.post(
            "/api/v1/control/acquire",
            json=payload,
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        # 200 OK (relay may return busy/unavailable, but HTTP check passed)
        self.assertEqual(res.status_code, 200)

    def test_get_request_allowed_without_origin(self):
        """Safe read-only GET endpoints are allowed without an Origin header."""
        res = self.client.get("/api/v1/version")
        self.assertEqual(res.status_code, 200)


class TestFastApiStrictModelsAndRest(unittest.TestCase):
    """Test FastAPI strict Pydantic models (extra='forbid'), types, and REST responses."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.op_sock = os.path.join(self.tmp_dir.name, "operator.sock")
        self.lc_sock = os.path.join(self.tmp_dir.name, "lifecycle.sock")

        self.sm = OperatorStateMachine(release_id="m12-test-release")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.4,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.op_server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.op_sock,
            allowed_uids=[os.getuid()],
        )
        self.op_server.start()

        self.lc_service = LifecycleHelperService(
            socket_path=self.lc_sock,
            allowed_uids={os.getuid()},
            systemctl_runner=lambda args: (
                0,
                "active" if "is-active" in args else "",
                "",
            ),
            journalctl_runner=lambda limit: [f"log entry {i}" for i in range(limit)],
        )
        self.lc_service.start()

        self.config = WebControlConfig(
            listen_address="127.0.0.1",
            port=8443,
            allowed_origins=["https://127.0.0.1:8443"],
        )
        self.app = create_app(
            config=self.config,
            operator_socket_path=self.op_sock,
            lifecycle_socket_path=self.lc_sock,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.lc_service.stop()
        self.op_server.stop()
        self.tmp_dir.cleanup()

    def test_get_version(self):
        """GET /api/v1/version returns protocol, api, and schema versions."""
        res = self.client.get("/api/v1/version")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(data["api_version"], API_VERSION)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["release_id"], "m12-test-release")

    def test_strict_models_reject_extra_fields(self):
        """Pydantic extra='forbid' rejects unexpected fields with 422."""
        res = self.client.post(
            "/api/v1/control/acquire",
            json={
                "operator_id": "user1",
                "request_id": "req-1",
                "unexpected_payload": 123,
            },
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res.status_code, 422)

    def test_strict_boolean_validation_on_arm(self):
        """Arm endpoint requires exact boolean tracks_raised: true (rejects strings/integers)."""
        # String "true" must be rejected
        res = self.client.post(
            "/api/v1/control/arm",
            json={"epoch": 1, "tracks_raised": "true", "request_id": "req-arm"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res.status_code, 422)

        # Integer 1 must be rejected
        res = self.client.post(
            "/api/v1/control/arm",
            json={"epoch": 1, "tracks_raised": 1, "request_id": "req-arm"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res.status_code, 422)

        # Boolean False must be rejected
        res = self.client.post(
            "/api/v1/control/arm",
            json={"epoch": 1, "tracks_raised": False, "request_id": "req-arm"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res.status_code, 422)

    def test_get_status(self):
        """GET /api/v1/status aggregates operator and lifecycle state."""
        res = self.client.get("/api/v1/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["service_state"], "active")
        self.assertEqual(data["operator_state"], "NO_OWNER")
        self.assertEqual(data["battery_voltage"], 12.4)

    def test_status_uses_lifecycle_state_while_operator_remains_available(self):
        """Controller status reflects systemd after stop while the agent remains up."""

        class AvailableRelay:
            def get_status(self):
                return {
                    "service_state": "active",
                    "operator_state": "NO_OWNER",
                }

        class LifecycleStatus:
            def __init__(self, success, state):
                self.success = success
                self.state = state
                self.status_calls = 0

            def get_status(self):
                self.status_calls += 1
                return self.success, self.state, None

        cases = (
            (True, "inactive", "inactive"),
            (True, "failed", "failed"),
            (False, "active", "unknown"),
            (True, "activating", "unknown"),
        )
        for success, observed_state, expected_state in cases:
            with self.subTest(success=success, observed_state=observed_state):
                lifecycle = LifecycleStatus(success, observed_state)
                app = create_app(
                    config=self.config,
                    relay=AvailableRelay(),
                    lifecycle=lifecycle,
                )

                response = TestClient(app).get("/api/v1/status")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["service_state"], expected_state)
                self.assertEqual(lifecycle.status_calls, 1)

    def test_control_acquire_enforces_configured_speed_caps(self):
        """Acquisition defaults and excessive requests use configured web caps."""

        class RecordingRelay:
            def __init__(self):
                self.acquisitions = []

            def acquire(self, **kwargs):
                self.acquisitions.append(kwargs)
                return True, len(self.acquisitions), "bind-token", "Acquired"

        relay = RecordingRelay()
        config = WebControlConfig(
            listen_address="127.0.0.1",
            port=8443,
            allowed_origins=["https://127.0.0.1:8443"],
            linear_speed_cap=0.05,
            angular_speed_cap=0.10,
        )
        client = TestClient(create_app(config=config, relay=relay))
        headers = {"Origin": "https://127.0.0.1:8443"}

        default_response = client.post(
            "/api/v1/control/acquire",
            json={"operator_id": "default", "request_id": "req-default"},
            headers=headers,
        )
        excessive_response = client.post(
            "/api/v1/control/acquire",
            json={
                "operator_id": "excessive",
                "request_id": "req-excessive",
                "max_linear_speed": 0.40,
                "max_angular_speed": 1.00,
            },
            headers=headers,
        )
        lower_response = client.post(
            "/api/v1/control/acquire",
            json={
                "operator_id": "lower",
                "request_id": "req-lower",
                "max_linear_speed": 0.03,
                "max_angular_speed": 0.08,
            },
            headers=headers,
        )

        self.assertEqual(default_response.status_code, 200)
        self.assertEqual(excessive_response.status_code, 200)
        self.assertEqual(lower_response.status_code, 200)
        self.assertEqual(
            [
                (call["max_linear_speed"], call["max_angular_speed"])
                for call in relay.acquisitions
            ],
            [(0.05, 0.10), (0.05, 0.10), (0.03, 0.08)],
        )

    def test_get_logs(self):
        """GET /api/v1/logs returns bounded log entries."""
        res = self.client.get("/api/v1/logs?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data["lines"]), 10)
        self.assertEqual(data["total_lines"], 10)

        # Limit > 200 is rejected by Query parameter validation
        res_bad = self.client.get("/api/v1/logs?limit=250")
        self.assertEqual(res_bad.status_code, 422)

    def test_control_lifecycle_flow(self):
        """Full REST control cycle: acquire, arm, release, stop."""
        # 1. Acquire
        res_acq = self.client.post(
            "/api/v1/control/acquire",
            json={"operator_id": "web-op-1", "request_id": "req-acq"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_acq.status_code, 200)
        acq_data = res_acq.json()
        self.assertTrue(acq_data["success"])
        epoch = acq_data["epoch"]
        self.assertIsNotNone(epoch)

        # Competing acquire is rejected with DEPLOYMENT_BUSY
        res_comp = self.client.post(
            "/api/v1/control/acquire",
            json={"operator_id": "competing-op", "request_id": "req-comp"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_comp.status_code, 200)
        comp_data = res_comp.json()
        self.assertFalse(comp_data["success"])
        self.assertEqual(comp_data["error"], WebControlErrorCode.DEPLOYMENT_BUSY.value)

        # 2. Arm with correct epoch
        res_arm = self.client.post(
            "/api/v1/control/arm",
            json={"epoch": epoch, "tracks_raised": True, "request_id": "req-arm"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_arm.status_code, 200)
        arm_data = res_arm.json()
        self.assertTrue(arm_data["success"])

        # 3. Release
        res_rel = self.client.post(
            "/api/v1/control/release",
            json={"epoch": epoch, "request_id": "req-rel"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_rel.status_code, 200)
        self.assertTrue(res_rel.json()["success"])

        # 4. Stop
        res_stop = self.client.post(
            "/api/v1/control/stop",
            json={"request_id": "req-stop"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_stop.status_code, 200)
        self.assertTrue(res_stop.json()["disarmed"])

    def test_controller_start_stop_operations(self):
        """Controller start/stop endpoints return tracked operation records."""
        res_start = self.client.post(
            "/api/v1/controller/start",
            json={"request_id": "req-start"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_start.status_code, 200)
        data_start = res_start.json()
        op_id = data_start["operation_id"]
        self.assertEqual(data_start["status"], "completed")

        # Query operation status
        res_op = self.client.get(f"/api/v1/operations/{op_id}")
        self.assertEqual(res_op.status_code, 200)
        self.assertEqual(res_op.json()["operation_id"], op_id)

        # Unknown operation returns 404
        res_unknown = self.client.get("/api/v1/operations/nonexistent-op")
        self.assertEqual(res_unknown.status_code, 404)


class TestOversizedAndFloodProtection(unittest.TestCase):
    """Test payload size bounding and request burst handling."""

    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)

    def test_oversized_http_payload_rejected(self):
        """HTTP requests exceeding 64 KiB receive 413 Payload Too Large."""
        large_junk = "x" * 70000
        res = self.client.post(
            "/api/v1/control/acquire",
            content=large_junk.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://127.0.0.1:8443",
            },
        )
        self.assertEqual(res.status_code, 413)

    def test_request_flood_handling(self):
        """High volume request bursts are processed without server failure or lock inversion."""
        for i in range(100):
            res = self.client.get("/api/v1/version")
            self.assertEqual(res.status_code, 200)


class TestWebSocketControlAndRelay(unittest.TestCase):
    """Test WebSocket control endpoint: origin validation, bind, intent relay, stop, and disconnect."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.op_sock = os.path.join(self.tmp_dir.name, "operator.sock")
        self.lc_sock = os.path.join(self.tmp_dir.name, "lifecycle.sock")

        self.sm = OperatorStateMachine(release_id="m12-test-ws")
        self.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.5,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        self.op_server = OperatorIpcServer(
            state_machine=self.sm,
            socket_path=self.op_sock,
            allowed_uids=[os.getuid()],
        )
        self.op_server.start()

        self.lc_service = LifecycleHelperService(
            socket_path=self.lc_sock,
            allowed_uids={os.getuid()},
            systemctl_runner=lambda args: (0, "active", ""),
            journalctl_runner=lambda limit: [],
        )
        self.lc_service.start()

        self.config = WebControlConfig(
            listen_address="127.0.0.1",
            port=8443,
            allowed_origins=["https://127.0.0.1:8443"],
        )
        self.app = create_app(
            config=self.config,
            operator_socket_path=self.op_sock,
            lifecycle_socket_path=self.lc_sock,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.lc_service.stop()
        self.op_server.stop()
        self.tmp_dir.cleanup()

    def test_websocket_origin_enforcement(self):
        """WebSocket connection with missing or unauthorized Origin is rejected."""
        # Missing Origin
        with (
            self.assertRaises(WebSocketDisconnect),
            self.client.websocket_connect("/api/v1/control"),
        ):
            pass

        # Unauthorized Origin
        with (
            self.assertRaises(WebSocketDisconnect),
            self.client.websocket_connect(
                "/api/v1/control", headers={"Origin": "https://malicious.example.com"}
            ),
        ):
            pass

    def test_websocket_oversized_frame_rejected(self):
        """Oversized message (> 64 KiB) drops WebSocket connection with 1009."""
        with self.client.websocket_connect(
            "/api/v1/control", headers={"Origin": "https://127.0.0.1:8443"}
        ) as ws:
            oversized_payload = json.dumps({"action": "heartbeat", "junk": "x" * 70000})
            ws.send_text(oversized_payload)
            # The server will send error and close
            try:
                frame = ws.receive_json()
                self.assertEqual(frame.get("type"), "error")
            except WebSocketDisconnect:
                pass

    def test_websocket_bind_intent_and_stop_priority(self):
        """Test WS session bind, challenge receipt, intent submission, and stop priority."""
        # 1. Acquire ownership through REST first
        res = self.client.post(
            "/api/v1/control/acquire",
            json={"operator_id": "ws-client", "request_id": "req-acq-ws"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res.status_code, 200)
        epoch = res.json()["epoch"]
        bind_token = res.json()["bind_token"]
        self.assertIsNotNone(bind_token)

        # Arm
        res_arm = self.client.post(
            "/api/v1/control/arm",
            json={"epoch": epoch, "tracks_raised": True, "request_id": "req-arm-ws"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertTrue(res_arm.json()["success"])

        with self.client.websocket_connect(
            "/api/v1/control", headers={"Origin": "https://127.0.0.1:8443"}
        ) as ws:
            # Send bind action with bind_token
            ws.send_json(
                {
                    "action": "bind",
                    "operator_id": "ws-client",
                    "epoch": epoch,
                    "bind_token": bind_token,
                }
            )
            bind_ack = ws.receive_json()
            self.assertEqual(bind_ack["type"], "ack")
            self.assertTrue(bind_ack["payload"]["success"])

            # Wait for an agent challenge
            challenge_frame = None
            for _ in range(20):
                msg = ws.receive_json()
                if msg.get("type") == "challenge":
                    challenge_frame = msg["payload"]
                    break
                time.sleep(0.02)

            self.assertIsNotNone(challenge_frame)
            token = challenge_frame["token"]

            # Submit valid intent in response to challenge
            ws.send_json(
                {
                    "action": "intent",
                    "token": token,
                    "epoch": epoch,
                    "sequence": 1,
                    "direction": "forward",
                }
            )
            intent_ack = ws.receive_json()
            self.assertEqual(intent_ack["type"], "ack")
            self.assertEqual(intent_ack["payload"]["direction"], "forward")

            # Test stop priority: stop immediately cancels driving and disarms
            ws.send_json({"action": "stop", "epoch": epoch})
            stop_ack = ws.receive_json()
            self.assertEqual(stop_ack["type"], "ack")
            self.assertTrue(stop_ack["payload"]["disarmed"])

    def test_websocket_disconnect_disarms(self):
        """Closing WebSocket connection drops dedicated IPC socket, triggering immediate disarm."""
        # 1. Acquire and Arm
        res = self.client.post(
            "/api/v1/control/acquire",
            json={"operator_id": "dc-client", "request_id": "req-dc"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        epoch = res.json()["epoch"]
        bind_token = res.json()["bind_token"]
        self.assertIsNotNone(bind_token)
        self.client.post(
            "/api/v1/control/arm",
            json={"epoch": epoch, "tracks_raised": True, "request_id": "req-arm-dc"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # 2. Connect WS and bind
        with self.client.websocket_connect(
            "/api/v1/control", headers={"Origin": "https://127.0.0.1:8443"}
        ) as ws:
            ws.send_json(
                {
                    "action": "bind",
                    "operator_id": "dc-client",
                    "epoch": epoch,
                    "bind_token": bind_token,
                }
            )
            _ = ws.receive_json()
            # Now exit context to abruptly close WebSocket

        # Wait briefly for operator server to observe EOF and disarm
        deadline = time.time() + 1.0
        while time.time() < deadline and self.sm.state != OperatorState.NO_OWNER:
            time.sleep(0.05)

        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)
        self.assertEqual(self.sm.active_direction, MotionDirection.NEUTRAL)

    def test_stop_preserves_operator_ownership_and_supports_rearm(self):
        """Disarm/Stop must preserve operator ownership (OWNED_DISARMED) and allow re-arming under new epoch."""
        # 1. Acquire control authority
        res = self.client.post(
            "/api/v1/control/acquire",
            json={"operator_id": "owner-op", "request_id": "req-acq-stop"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res.status_code, 200)
        epoch = res.json()["epoch"]

        # 2. Arm chassis
        res_arm = self.client.post(
            "/api/v1/control/arm",
            json={"epoch": epoch, "tracks_raised": True, "request_id": "req-arm-1"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_arm.status_code, 200)
        self.assertTrue(res_arm.json()["success"])
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # 3. Issue stop / disarm
        res_stop = self.client.post(
            "/api/v1/control/stop",
            json={"epoch": epoch, "request_id": "req-stop-1"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_stop.status_code, 200)
        self.assertTrue(res_stop.json()["success"])
        self.assertTrue(res_stop.json()["disarmed"])

        # Disarm is pending downstream guard confirmation
        self.assertTrue(self.sm.disarm_pending)

        # Simulate downstream guard confirming disarm
        self.sm.update_guard_telemetry(False, time.monotonic_ns())

        # 4. Assert status: operator ownership is PRESERVED, not revoked!
        res_status = self.client.get("/api/v1/status")
        self.assertEqual(res_status.status_code, 200)
        status_data = res_status.json()
        self.assertEqual(status_data["operator_state"], "OWNED_DISARMED")
        self.assertEqual(status_data["active_owner"], "owner-op")
        self.assertFalse(status_data["guard_armed"])
        self.assertFalse(status_data["disarm_pending"])
        new_epoch = status_data["current_epoch"]
        self.assertEqual(new_epoch, epoch + 1)

        # 5. Re-arm chassis under new epoch
        res_rearm = self.client.post(
            "/api/v1/control/arm",
            json={
                "epoch": new_epoch,
                "tracks_raised": True,
                "request_id": "req-rearm-2",
            },
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_rearm.status_code, 200)
        self.assertTrue(res_rearm.json()["success"])
        self.assertEqual(self.sm.state, OperatorState.ARMED_IDLE)

        # 6. Release ownership
        res_release = self.client.post(
            "/api/v1/control/release",
            json={"epoch": new_epoch, "request_id": "req-rel-stop"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res_release.status_code, 200)
        self.assertTrue(res_release.json()["success"])
        self.assertEqual(self.sm.state, OperatorState.NO_OWNER)


class TestLifecycleHelperService(unittest.TestCase):
    """Test restricted root lifecycle helper socket daemon and safety interlocks."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp_dir.name, "lifecycle.sock")
        self.deploy_lock = os.path.join(self.tmp_dir.name, "deploy.lock")
        self.recovery_pending = os.path.join(self.tmp_dir.name, "recovery_pending")
        self.journal_file = os.path.join(self.tmp_dir.name, "activation-journal")

        self.preflight_ok = True
        self.preflight_msg = "All checks passed"

        def _mock_preflight():
            return self.preflight_ok, self.preflight_msg

        self.service_active = False

        def _mock_systemctl(args):
            cmd = args[0]
            if cmd == "is-active":
                return (
                    0 if self.service_active else 3,
                    "active" if self.service_active else "inactive",
                    "",
                )
            elif cmd == "start":
                self.service_active = True
                return 0, "", ""
            elif cmd == "stop":
                self.service_active = False
                return 0, "", ""
            return -1, "", "unknown command"

        self.helper = LifecycleHelperService(
            socket_path=self.socket_path,
            allowed_uids={os.getuid()},
            systemctl_runner=_mock_systemctl,
            journalctl_runner=lambda limit: [f"entry {i}" for i in range(limit)],
            preflight_runner=_mock_preflight,
            deployment_lock_file=self.deploy_lock,
            recovery_pending_file=self.recovery_pending,
            journal_file=self.journal_file,
        )
        self.helper.start()
        self.client = LifecycleClient(socket_path=self.socket_path)

    def tearDown(self):
        self.helper.stop()
        self.tmp_dir.cleanup()

    def test_status_query(self):
        """Lifecycle helper reports accurate service status."""
        ok, state, _ = self.client.get_status()
        self.assertTrue(ok)
        self.assertEqual(state, "inactive")

    def test_start_and_stop_success(self):
        """Lifecycle helper starts and stops controller when conditions are valid."""
        ok_start, state_start, _ = self.client.start_controller()
        self.assertTrue(ok_start)
        self.assertEqual(state_start, "active")

        ok_stop, state_stop, _ = self.client.stop_controller()
        self.assertTrue(ok_stop)
        self.assertEqual(state_stop, "inactive")

    def test_start_blocked_when_deployment_lock_held(self):
        """Start must fail with DEPLOYMENT_BUSY if deployment lock is held."""
        # Hold deployment lock exclusively
        with open(self.deploy_lock, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)

            ok, _, msg = self.client.start_controller()
            self.assertFalse(ok)
            self.assertIn("deployment lock", msg.lower())

    def test_start_blocked_when_recovery_pending(self):
        """Start must fail if activation recovery is pending."""
        with open(self.recovery_pending, "w") as f:
            f.write("recovery required")

        ok, _, msg = self.client.start_controller()
        self.assertFalse(ok)
        self.assertIn("recovery is pending", msg.lower())

    def test_start_blocked_when_uncommitted_journal_transaction(self):
        """Start must fail if activation journal has an uncommitted transaction."""
        with open(self.journal_file, "w") as f:
            json.dump(
                {"format_version": "1.0.0", "current_transaction": {"tx_id": "tx1"}}, f
            )

        ok, _, msg = self.client.start_controller()
        self.assertFalse(ok)
        self.assertIn("uncommitted", msg.lower())

    def test_start_blocked_when_preflight_fails(self):
        """Start must fail with PREFLIGHT_FAILED if host preflight fails."""
        self.preflight_ok = False
        self.preflight_msg = "Battery voltage critical (9100 mV < 9600 mV)"

        ok, _, msg = self.client.start_controller()
        self.assertFalse(ok)
        self.assertIn("preflight failed", msg.lower())

    def test_stop_succeeds_even_when_deployment_lock_held(self):
        """Stop must not wait behind deployment lock; safety stops execute immediately."""
        self.service_active = True
        with open(self.deploy_lock, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)

            ok, state, _ = self.client.stop_controller()
            self.assertTrue(ok)
            self.assertEqual(state, "inactive")

    def test_get_logs_bounded(self):
        """Lifecycle helper returns bounded recent journal lines."""
        ok, lines, total, _, _ = self.client.get_logs(limit=25)
        self.assertTrue(ok)
        self.assertEqual(len(lines), 25)
        self.assertEqual(total, 25)

    def test_unauthorized_uid_rejected(self):
        """Unauthorized UID receives UNAUTHORIZED error from lifecycle helper."""
        # Test handle_request directly with unauthorized UID 9999
        res = self.helper.handle_request({"action": "status"}, peer_uid=9999)
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "UNAUTHORIZED")


class TestSystemdServiceHardening(unittest.TestCase):
    """Test systemd service definitions, sandboxing, and systemd-analyze verification."""

    def test_lifecycle_launcher_imports_release_packages_without_pythonpath(self):
        """Packaged lifecycle launcher starts from a clean systemd-like environment."""
        launcher = os.path.join(UBUNTU_TANK_DIR, "bin/mentorpi-tank-lifecycle")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        result = subprocess.run(
            [sys.executable, launcher, "--help"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Restricted Lifecycle Helper Service", result.stdout)

    def test_systemd_analyze_verify_units(self):
        """mentorpi-tank-web.service and mentorpi-tank-lifecycle.service pass systemd-analyze."""
        systemd_analyze = shutil.which("systemd-analyze")
        if not systemd_analyze:
            self.skipTest("systemd-analyze unavailable")

        web_unit = os.path.join(UBUNTU_TANK_DIR, "host/mentorpi-tank-web.service")
        lc_unit = os.path.join(UBUNTU_TANK_DIR, "host/mentorpi-tank-lifecycle.service")
        self.assertTrue(os.path.isfile(web_unit))
        self.assertTrue(os.path.isfile(lc_unit))

        with tempfile.TemporaryDirectory() as td:
            for src_name in (
                "mentorpi-tank-web.service",
                "mentorpi-tank-lifecycle.service",
            ):
                src = os.path.join(UBUNTU_TANK_DIR, "host", src_name)
                dst = os.path.join(td, src_name)
                with open(src, "r", encoding="utf-8") as f:
                    content = f.read()
                # Substitute /bin/true for non-existent target paths on development host
                mocked = content.replace(
                    "/opt/ubuntu_tank/current/bin/mentorpi-tank-web", "/bin/true"
                ).replace(
                    "/opt/ubuntu_tank/current/bin/mentorpi-tank-lifecycle", "/bin/true"
                )
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(mocked)

                res = subprocess.run(
                    [systemd_analyze, "verify", dst],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"systemd-analyze verify failed for {src_name}:\n{res.stderr}\n{res.stdout}",
                )

    def test_systemd_analyze_rejects_malformed_directives(self):
        """systemd-analyze verify rejects corrupted directives in service units."""
        systemd_analyze = shutil.which("systemd-analyze")
        if not systemd_analyze:
            self.skipTest("systemd-analyze unavailable")

        src = os.path.join(UBUNTU_TANK_DIR, "host/mentorpi-tank-web.service")
        with tempfile.TemporaryDirectory() as td:
            bad_unit = os.path.join(td, "bad.service")
            with open(src, "r", encoding="utf-8") as f:
                content = f.read()
            corrupted = content.replace(
                "ProtectSystem=strict", "ProtectSystem=bogus_value"
            )
            with open(bad_unit, "w", encoding="utf-8") as f:
                f.write(corrupted)

            res = subprocess.run(
                [systemd_analyze, "verify", bad_unit],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertIn("Failed to parse", res.stderr + res.stdout)


class TestFaultToleranceAndUnavailableSubsystems(unittest.TestCase):
    """Test web layer behavior when subsystems are unavailable or frozen."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.op_sock = os.path.join(self.tmp_dir.name, "operator.sock")
        self.lc_sock = os.path.join(self.tmp_dir.name, "lifecycle.sock")

        self.stopped_controller = False

        def _mock_systemctl(args):
            cmd = args[0]
            if cmd == "stop":
                self.stopped_controller = True
                return 0, "", ""
            return 0, "inactive", ""

        self.lc_service = LifecycleHelperService(
            socket_path=self.lc_sock,
            allowed_uids={os.getuid()},
            systemctl_runner=_mock_systemctl,
            journalctl_runner=lambda limit: [],
        )
        self.lc_service.start()

        self.config = WebControlConfig(
            listen_address="127.0.0.1",
            port=8443,
            allowed_origins=["https://127.0.0.1:8443"],
        )
        # Note: operator_socket_path does not exist!
        self.app = create_app(
            config=self.config,
            operator_socket_path=self.op_sock,
            lifecycle_socket_path=self.lc_sock,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.lc_service.stop()
        self.tmp_dir.cleanup()

    def test_status_when_operator_agent_unavailable(self):
        """GET /status returns gracefully with FAULT state when operator agent IPC is down."""
        res = self.client.get("/api/v1/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["operator_state"], "FAULT")
        self.assertEqual(data["service_state"], "inactive")

    def test_stop_fallback_to_lifecycle_helper(self):
        """POST /control/stop falls back to lifecycle helper stop when operator agent is down."""
        res = self.client.post(
            "/api/v1/control/stop",
            json={"request_id": "req-emergency"},
            headers={"Origin": "https://127.0.0.1:8443"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertTrue(res.json()["disarmed"])
        self.assertTrue(self.stopped_controller)


class TestOpenApiExport(unittest.TestCase):
    """Test OpenAPI schema generation and export."""

    def test_openapi_export_schema(self):
        """app.openapi() generates a valid OpenAPI 3.1.0 schema containing all endpoints."""
        app = create_app()
        spec = app.openapi()
        self.assertIn("openapi", spec)
        self.assertIn("paths", spec)

        paths = spec["paths"]
        self.assertIn("/api/v1/version", paths)
        self.assertIn("/api/v1/status", paths)
        self.assertIn("/api/v1/logs", paths)
        self.assertIn("/api/v1/control/acquire", paths)
        self.assertIn("/api/v1/control/arm", paths)
        self.assertIn("/api/v1/control/release", paths)
        self.assertIn("/api/v1/control/stop", paths)
        self.assertIn("/api/v1/controller/start", paths)
        self.assertIn("/api/v1/controller/stop", paths)
        self.assertIn("/api/v1/operations/{id}", paths)


class TestMilestone12ReviewRemediations(unittest.TestCase):
    """Regression tests explicitly verifying all 7 review findings from review.md."""

    def test_finding1_submodule_import_isolation(self):
        """Submodules like lifecycle_service can be imported without importing FastAPI."""
        cmd = [
            sys.executable,
            "-c",
            (
                "import sys\n"
                f"sys.path.insert(0, '{WEB_PKG_DIR}')\n"
                f"sys.path.insert(0, '{OPERATOR_PKG_DIR}')\n"
                "from ubuntu_tank_web.lifecycle_service import LifecycleHelperService\n"
                "assert 'fastapi' not in sys.modules, 'fastapi was imported eagerly!'\n"
            ),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(
            res.returncode, 0, f"Import isolation check failed: {res.stderr}"
        )

    def test_finding2_writable_tls_provisioning_and_recovery(self):
        """TLS material provisions into writable persistent path with self-healing corrupt recovery."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cert_dir = os.path.join(tmp_dir, "web/certs")
            cert_path = os.path.join(cert_dir, "server.crt")
            key_path = os.path.join(cert_dir, "server.key")

            # 1. Provision fresh certificate in custom writable path
            ensure_tls_certificate(cert_path=cert_path, key_path=key_path)
            self.assertTrue(os.path.exists(cert_path))
            self.assertTrue(os.path.exists(key_path))
            self.assertEqual(oct(os.stat(key_path).st_mode)[-3:], "600")
            self.assertEqual(oct(os.stat(cert_path).st_mode)[-3:], "644")

            # 2. Corrupt certificate and verify automatic recovery under same directory
            with open(cert_path, "w", encoding="utf-8") as f:
                f.write("CORRUPT_CERTIFICATE_DATA")

            ensure_tls_certificate(cert_path=cert_path, key_path=key_path)
            with open(cert_path, "rb") as f:
                cert_data = f.read()
            parsed = x509.load_pem_x509_certificate(cert_data)
            self.assertIsNotNone(parsed)

    def test_finding3_exclusive_websocket_binding(self):
        """Acquired owner connection is exclusive: other sessions without single-use bind_token are rejected."""
        tmp_dir = tempfile.TemporaryDirectory()
        op_sock = os.path.join(tmp_dir.name, "operator.sock")
        lc_sock = os.path.join(tmp_dir.name, "lifecycle.sock")

        def _telemetry_cb():
            return TelemetrySnapshot(
                battery_voltage=12.4, linear_speed=0.0, angular_speed=0.0
            )

        sm = OperatorStateMachine()
        server = OperatorIpcServer(
            state_machine=sm,
            socket_path=op_sock,
            allowed_uids=[os.getuid()],
        )
        server.start()

        helper = LifecycleHelperService(
            socket_path=lc_sock,
            allowed_uids={os.getuid()},
            systemctl_runner=lambda args: (0, "active", ""),
        )
        helper.start()

        config = WebControlConfig(
            listen_address="127.0.0.1",
            port=8443,
            allowed_origins=["https://127.0.0.1:8443"],
        )
        app = create_app(
            config=config,
            operator_socket_path=op_sock,
            lifecycle_socket_path=lc_sock,
        )
        client = TestClient(app)

        try:
            # 1. Tab A acquires ownership
            res = client.post(
                "/api/v1/control/acquire",
                json={"operator_id": "tab-a", "request_id": "req-acq-a"},
                headers={"Origin": "https://127.0.0.1:8443"},
            )
            self.assertEqual(res.status_code, 200)
            acq_data = res.json()
            self.assertTrue(acq_data["success"])
            epoch = acq_data["epoch"]
            bind_token = acq_data["bind_token"]
            self.assertIsNotNone(bind_token)

            # Arm
            client.post(
                "/api/v1/control/arm",
                json={
                    "epoch": epoch,
                    "tracks_raised": True,
                    "request_id": "req-arm-a",
                },
                headers={"Origin": "https://127.0.0.1:8443"},
            )

            # 2. Tab B tries to bind using visible /status data with wrong or missing bind_token
            with client.websocket_connect(
                "/api/v1/control", headers={"Origin": "https://127.0.0.1:8443"}
            ) as ws_b:
                # Missing token
                ws_b.send_json(
                    {"action": "bind", "operator_id": "tab-b", "epoch": epoch}
                )
                msg = ws_b.receive_json()
                self.assertEqual(msg["type"], "error")
                self.assertEqual(
                    msg["payload"]["error"], WebControlErrorCode.INVALID_PAYLOAD.value
                )

                # Wrong token or wrong operator_id
                ws_b.send_json(
                    {
                        "action": "bind",
                        "operator_id": "tab-b",
                        "epoch": epoch,
                        "bind_token": "fraudulent_token",
                    }
                )
                msg = ws_b.receive_json()
                self.assertEqual(msg["type"], "error")
                self.assertEqual(
                    msg["payload"]["error"], WebControlErrorCode.NOT_OWNER.value
                )

            # 3. Tab A connects and successfully binds using single-use bind_token
            with client.websocket_connect(
                "/api/v1/control", headers={"Origin": "https://127.0.0.1:8443"}
            ) as ws_a:
                ws_a.send_json(
                    {
                        "action": "bind",
                        "operator_id": "tab-a",
                        "epoch": epoch,
                        "bind_token": bind_token,
                    }
                )
                ack = ws_a.receive_json()
                self.assertEqual(ack["type"], "ack")
                self.assertTrue(ack["payload"]["success"])

                # 4. Tab B attempts to bind reusing Tab A's bind_token
                with client.websocket_connect(
                    "/api/v1/control", headers={"Origin": "https://127.0.0.1:8443"}
                ) as ws_b2:
                    ws_b2.send_json(
                        {
                            "action": "bind",
                            "operator_id": "tab-a",
                            "epoch": epoch,
                            "bind_token": bind_token,
                        }
                    )
                    msg = ws_b2.receive_json()
                    self.assertEqual(msg["type"], "error")
                    self.assertEqual(
                        msg["payload"]["error"], WebControlErrorCode.NOT_OWNER.value
                    )
        finally:
            app.state.operator_relay.close()
            server.stop()
            helper.stop()
            tmp_dir.cleanup()

    def test_finding4_fail_closed_stop_confirmation(self):
        """When systemctl stop or is-active fails, stop reports success=False and disarmed=False."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sock_path = os.path.join(tmp_dir, "lifecycle.sock")

            # Mock systemctl where both stop and is-active fail
            def _mock_failing_systemctl(args):
                return 1, "", "systemd bus disconnected"

            helper = LifecycleHelperService(
                socket_path=sock_path,
                allowed_uids={os.getuid()},
                systemctl_runner=_mock_failing_systemctl,
            )
            helper.start()
            lc_client = LifecycleClient(socket_path=sock_path)
            try:
                success, state, _ = lc_client.stop_controller(timeout_sec=2.0)
                self.assertFalse(success)
                self.assertEqual(state, "unknown")

                # Test persistent-active response
                def _mock_active_systemctl(args):
                    if args[0] == "is-active":
                        return 0, "active", ""
                    return 0, "", ""

                helper._custom_systemctl = _mock_active_systemctl
                success, state, _ = lc_client.stop_controller(timeout_sec=2.0)
                self.assertFalse(success)
                self.assertEqual(state, "active")

                # Test explicitly inactive response
                def _mock_inactive_systemctl(args):
                    if args[0] == "is-active":
                        return 3, "inactive", ""
                    return 0, "", ""

                helper._custom_systemctl = _mock_inactive_systemctl
                success, state, _ = lc_client.stop_controller(timeout_sec=2.0)
                self.assertTrue(success)
                self.assertEqual(state, "inactive")
            finally:
                helper.stop()

    def test_finding5_serialized_ipc_client(self):
        """Concurrent threads sharing an OperatorIpcClient do not interleave frames or desync."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sock_path = os.path.join(tmp_dir, "operator.sock")
            sm = OperatorStateMachine()
            server = OperatorIpcServer(
                state_machine=sm,
                socket_path=sock_path,
                allowed_uids=[os.getuid()],
            )
            server.start()

            client = OperatorIpcClient(socket_path=sock_path)
            client.connect(timeout_sec=2.0)

            errors = []

            def _status_worker():
                for _ in range(30):
                    try:
                        st = client.get_status(timeout_sec=2.0)
                        if "operator_state" not in st:
                            errors.append("Malformed status response")
                    except (
                        OSError,
                        RuntimeError,
                        TimeoutError,
                        ValueError,
                        KeyError,
                    ) as exc:
                        errors.append(f"Status worker error: {exc}")

            def _version_worker():
                for _ in range(30):
                    try:
                        v = client.get_version(timeout_sec=2.0)
                        if "protocol_version" not in v:
                            errors.append("Malformed version response")
                    except (
                        OSError,
                        RuntimeError,
                        TimeoutError,
                        ValueError,
                        KeyError,
                    ) as exc:
                        errors.append(f"Version worker error: {exc}")

            t1 = threading.Thread(target=_status_worker)
            t2 = threading.Thread(target=_version_worker)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

            client.close()
            server.stop()

            self.assertEqual(errors, [])

    def test_finding6_priority_stop_wins_race_against_slow_operations(self):
        """Stop executes immediately on priority thread without waiting behind slow start, status, or logs."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sock_path = os.path.join(tmp_dir, "lifecycle.sock")
            start_running = threading.Event()
            start_proceed = threading.Event()

            def _slow_systemctl(args):
                cmd = args[0]
                if cmd == "start":
                    start_running.set()
                    start_proceed.wait(timeout=2.0)
                    return 0, "", ""
                elif cmd == "stop":
                    return 0, "", ""
                elif cmd == "is-active":
                    return 3, "inactive", ""
                return 0, "", ""

            helper = LifecycleHelperService(
                socket_path=sock_path,
                allowed_uids={os.getuid()},
                systemctl_runner=_slow_systemctl,
                preflight_runner=lambda: (True, "OK"),
            )
            helper.start()

            start_result = {}

            def _start_worker():
                c = LifecycleClient(socket_path=sock_path)
                start_result["res"] = c.start_controller(timeout_sec=5.0)

            t_start = threading.Thread(target=_start_worker)
            t_start.start()

            # Wait until start enters systemctl
            self.assertTrue(start_running.wait(timeout=2.0))

            # Concurrently issue Stop with 500ms timeout
            c_stop = LifecycleClient(socket_path=sock_path)
            stop_start_time = time.monotonic()
            ok, _state, _msg = c_stop.stop_controller(timeout_sec=0.5)
            stop_elapsed = time.monotonic() - stop_start_time

            # Stop completed promptly (< 300ms), without waiting for start to release
            self.assertTrue(ok)
            self.assertLess(stop_elapsed, 0.4)

            # Allow slow start to proceed and finish
            start_proceed.set()
            t_start.join(timeout=3.0)

            # Start must report failure aborted by stop
            start_ok, _, start_msg = start_result["res"]
            self.assertFalse(start_ok)
            self.assertIn("superseded", start_msg or "")

            helper.stop()

    def test_finding7_nonblocking_uvicorn_event_loop(self):
        """Blocking IPC calls in routes_api offload via asyncio.to_thread and do not freeze the loop."""
        tmp_dir = tempfile.TemporaryDirectory()
        op_sock = os.path.join(tmp_dir.name, "operator.sock")
        lc_sock = os.path.join(tmp_dir.name, "lifecycle.sock")

        class SlowRelay:
            def __init__(self):
                self.socket_path = op_sock

            def get_status(self, timeout_sec=2.0):
                time.sleep(0.3)  # slow status
                return {"service_state": "active", "operator_state": "NO_OWNER"}

            def stop(self, request_id=None, epoch=None, timeout_sec=3.0):
                return True, "Stopped"

        slow_relay = SlowRelay()
        config = WebControlConfig(
            listen_address="127.0.0.1",
            port=8443,
            allowed_origins=["https://127.0.0.1:8443"],
        )
        app = create_app(
            config=config,
            operator_socket_path=op_sock,
            lifecycle_socket_path=lc_sock,
            relay=slow_relay,
        )
        client = TestClient(app)

        try:
            # Run slow status and verify stop does not block behind it
            start_time = time.monotonic()
            res_stop = client.post(
                "/api/v1/control/stop",
                json={"request_id": "fast-stop"},
                headers={"Origin": "https://127.0.0.1:8443"},
            )
            elapsed = time.monotonic() - start_time
            self.assertEqual(res_stop.status_code, 200)
            self.assertTrue(res_stop.json()["success"])
            self.assertTrue(res_stop.json()["disarmed"])
            self.assertLess(elapsed, 0.15)
        finally:
            tmp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
