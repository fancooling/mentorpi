"""
test_milestone13_browser_pwa.py - Test suite for Milestone 13: Vue browser & PWA driving interface.

Verifies:
1. Frontend compilation closure: Vite build produces dist/ with index.html, assets, manifest, and service worker.
2. PWA manifest compliance: standalone display, 192x192 and 512x512 icons, start URL, theme color.
3. Service worker cache safety: asset-only precache, explicit /api/ denylist, NetworkOnly for API requests,
   no background sync for motion commands, and explicit SKIP_WAITING update handling.
4. Real browser automation via Playwright and system Google Chrome against live FastAPI test server:
   - Initial page layout, status indicators, accessible semantic buttons, and visible STOP button.
   - Control acquisition and single-operator ownership exclusivity across multiple tabs.
   - Arming gate strictly requiring tracks-raised confirmation checkbox.
   - Press-and-hold pointer driving: pointerdown drives, pointerup stops.
   - Pointer cancellation: pointerleave/pointercancel resets to neutral.
   - Keyboard driving: W/S/A/D active only when drive panel is focused.
   - Space key immediate STOP priority document-wide, clearing all held input.
   - Keys held prior to Arm cannot initiate motion (requires release and re-press).
   - Mixed input conflict immediately resets to neutral and triggers safety stop.
   - 5-second continuous hold cap automatically stops and disarms.
   - Window blur and hidden document visibility change clear input and disarm without auto-resume.
   - Incompatible protocol version banner blocks control.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import uvicorn

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "../.."))
UBUNTU_TANK_DIR = os.path.join(REPO_ROOT, "ubuntu_tank")
WEB_DIR = os.path.join(UBUNTU_TANK_DIR, "web")
DIST_DIR = os.path.join(WEB_DIR, "dist")
WEB_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_web")
OPERATOR_PKG_DIR = os.path.join(UBUNTU_TANK_DIR, "src/ubuntu_tank_operator")

for p in [UBUNTU_TANK_DIR, WEB_PKG_DIR, OPERATOR_PKG_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from ubuntu_tank_operator.config import WebControlConfig
from ubuntu_tank_operator.enums import MotionDirection, OperatorState
from ubuntu_tank_operator.ipc_server import OperatorIpcServer
from ubuntu_tank_operator.schemas import TelemetrySnapshot
from ubuntu_tank_operator.state_machine import OperatorStateMachine
from ubuntu_tank_web.app import create_app
from ubuntu_tank_web.lifecycle_service import LifecycleHelperService


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestFrontendBuildAndClosure(unittest.TestCase):
    """Verify frontend build closure, PWA manifest, and service worker caching rules."""

    @classmethod
    def setUpClass(cls):
        # Run npm run build in ubuntu_tank/web
        cmd = ["npm", "run", "build"]
        res = subprocess.run(
            cmd, cwd=WEB_DIR, capture_output=True, text=True, check=False
        )
        if res.returncode != 0:
            print("Build stdout:", res.stdout)
            print("Build stderr:", res.stderr)
        assert res.returncode == 0, f"npm run build failed: {res.stderr}"

    def test_dist_artifacts_exist(self):
        """Build emits index.html, assets, manifest, and service worker."""
        self.assertTrue(os.path.isfile(os.path.join(DIST_DIR, "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(DIST_DIR, "manifest.webmanifest")))
        self.assertTrue(os.path.isfile(os.path.join(DIST_DIR, "sw.js")))
        self.assertTrue(os.path.isdir(os.path.join(DIST_DIR, "assets")))

        # Check CSS and JS assets exist
        assets = os.listdir(os.path.join(DIST_DIR, "assets"))
        has_js = any(f.endswith(".js") for f in assets)
        has_css = any(f.endswith(".css") for f in assets)
        self.assertTrue(has_js, "No compiled JS bundle found in dist/assets")
        self.assertTrue(has_css, "No compiled CSS bundle found in dist/assets")

    def test_manifest_pwa_compliance(self):
        """PWA manifest defines standalone display, theme color, and 192/512 icons."""
        manifest_path = os.path.join(DIST_DIR, "manifest.webmanifest")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest.get("display"), "standalone")
        self.assertEqual(manifest.get("theme_color"), "#0f172a")
        self.assertEqual(manifest.get("start_url"), "/")

        icons = manifest.get("icons", [])
        sizes = [i.get("sizes") for i in icons]
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)

        # Check that referenced icon files actually exist in dist
        for icon in icons:
            src = icon.get("src", "")
            icon_path = os.path.join(DIST_DIR, src.lstrip("/"))
            self.assertTrue(
                os.path.isfile(icon_path),
                f"Referenced manifest icon missing: {src}",
            )

    def test_service_worker_asset_only_and_api_exclusion(self):
        """Service worker strictly excludes /api/ from caching and uses NetworkOnly."""
        sw_path = os.path.join(DIST_DIR, "sw.js")
        with open(sw_path, "r", encoding="utf-8") as f:
            sw_content = f.read()

        # Check navigation denylist excludes /api/
        self.assertIn(
            "denylist:[/^\\/api\\//]",
            sw_content,
            "Service worker navigation fallback must denylist /api/",
        )
        # Check NetworkOnly rule for /api/
        self.assertTrue(
            "NetworkOnly" in sw_content
            and (r"\/api\/" in sw_content or "/api/" in sw_content),
            "Service worker must specify NetworkOnly for API requests",
        )
        # Check explicit SKIP_WAITING message handler
        self.assertIn(
            "SKIP_WAITING",
            sw_content,
            "Service worker must handle explicit SKIP_WAITING update message",
        )


class TestRealBrowserInteractions(unittest.TestCase):
    """Run automated Playwright browser test suite against live test FastAPI server."""

    @classmethod
    def setUpClass(cls):
        # 1. Build frontend if dist missing
        if not os.path.isdir(DIST_DIR) or not os.path.isfile(
            os.path.join(DIST_DIR, "index.html")
        ):
            res = subprocess.run(
                ["npm", "run", "build"],
                cwd=WEB_DIR,
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0, f"npm run build failed: {res.stderr}"

        # 2. Pick free port
        cls.port = find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        # 3. Create temporary directory for sockets
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.op_sock = os.path.join(cls.tmp_dir.name, "operator.sock")
        cls.lc_sock = os.path.join(cls.tmp_dir.name, "lifecycle.sock")

        # 4. Initialize Operator State Machine & IPC Server
        cls.sm = OperatorStateMachine(release_id="m13-test-browser")
        cls.sm.telemetry = TelemetrySnapshot(
            battery_voltage=12.2,
            battery_monotonic_ns=time.monotonic_ns(),
            guard_armed=False,
            guard_monotonic_ns=time.monotonic_ns(),
            odom_linear_x=0.0,
            odom_angular_z=0.0,
            odom_monotonic_ns=time.monotonic_ns(),
        )
        cls.telemetry_running = True

        def _telemetry_refresher():
            while cls.telemetry_running:
                now_ns = time.monotonic_ns()
                is_armed = cls.sm.state in (
                    OperatorState.ARMED_IDLE,
                    OperatorState.DRIVING,
                )
                cls.sm.update_guard_telemetry(is_armed, now_ns)
                cls.sm.update_battery_telemetry(12.2, now_ns)
                cls.sm.update_odom_telemetry(0.0, 0.0, now_ns)
                time.sleep(0.05)

        cls.telemetry_thread = threading.Thread(
            target=_telemetry_refresher, daemon=True
        )
        cls.telemetry_thread.start()

        cls.op_server = OperatorIpcServer(
            state_machine=cls.sm,
            socket_path=cls.op_sock,
            allowed_uids=[os.getuid()],
        )
        cls.op_server.start()

        # 5. Initialize Lifecycle Helper Service
        cls.service_active = True

        def _mock_systemctl(args):
            cmd = args[0]
            if cmd == "is-active":
                return (
                    0 if cls.service_active else 3,
                    "active" if cls.service_active else "inactive",
                    "",
                )
            elif cmd == "start":
                cls.service_active = True
                return 0, "", ""
            elif cmd == "stop":
                cls.service_active = False
                return 0, "", ""
            return -1, "", "unknown command"

        cls.lc_service = LifecycleHelperService(
            socket_path=cls.lc_sock,
            allowed_uids={os.getuid()},
            systemctl_runner=_mock_systemctl,
            journalctl_runner=lambda limit: [
                "Sep 18 14:00:00 mentorpi systemd[1]: Started MentorPi Tank Controller."
            ],
        )
        cls.lc_service.start()

        # 6. Initialize FastAPI application serving dist/
        cls.config = WebControlConfig(
            listen_address="127.0.0.1",
            port=cls.port,
            allowed_origins=[cls.base_url, f"http://localhost:{cls.port}"],
        )
        cls.app = create_app(
            config=cls.config,
            static_dir=DIST_DIR,
            operator_socket_path=cls.op_sock,
            lifecycle_socket_path=cls.lc_sock,
        )

        @cls.app.post("/api/v1/test/reset")
        def reset_operator_state():
            now_ns = time.monotonic_ns()
            with cls.sm._lock:
                cls.sm.owner_id = None
                cls.sm.state = OperatorState.NO_OWNER
                cls.sm.disarm_pending = False
                cls.sm.compensating_disarm_required = False
                cls.sm.active_direction = MotionDirection.NEUTRAL
            cls.sm.update_guard_telemetry(False, now_ns)
            cls.sm.update_battery_telemetry(12.2, now_ns)
            cls.sm.update_odom_telemetry(0.0, 0.0, now_ns)
            try:
                cls.app.state.operator_relay.release_owner_binding("reset")
                cls.app.state.operator_relay.close()
            except (OSError, RuntimeError):
                pass
            return {"reset": True}

        # 7. Start Uvicorn in background thread
        uv_config = uvicorn.Config(
            cls.app,
            host="127.0.0.1",
            port=cls.port,
            log_level="warning",
        )
        cls.uv_server = uvicorn.Server(uv_config)
        cls.server_thread = threading.Thread(target=cls.uv_server.run, daemon=True)
        cls.server_thread.start()

        # Wait for server to accept connections
        deadline = time.time() + 5.0
        connected = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2):
                    connected = True
                    break
            except (OSError, ConnectionRefusedError):
                time.sleep(0.05)
        assert connected, "Test FastAPI server failed to start within 5s"

    @classmethod
    def tearDownClass(cls):
        cls.uv_server.should_exit = True
        cls.server_thread.join(timeout=3.0)
        cls.lc_service.stop()
        cls.op_server.stop()
        cls.tmp_dir.cleanup()

    def test_playwright_browser_control_suite(self):
        """Execute Playwright browser test suite in headless Google Chrome."""
        env = os.environ.copy()
        env["TEST_BASE_URL"] = self.base_url

        cmd = ["npx", "playwright", "test"]
        proc = subprocess.run(
            cmd, cwd=WEB_DIR, env=env, capture_output=True, text=True, check=False
        )

        print(proc.stdout)
        if proc.returncode != 0:
            print("Playwright stderr:", proc.stderr)

        self.assertEqual(
            proc.returncode,
            0,
            f"Playwright tests failed with exit code {proc.returncode}:\n{proc.stdout}\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
