"""
Operator IPC Client for MentorPi Pi 5 Web Control.

Provides a robust, synchronous Python client for communicating with the
operator agent over Unix domain sockets with newline-delimited JSON framing.
"""

from __future__ import annotations

import json
import math
import os
import socket
import threading
import time
import uuid
from typing import Any

from .constants import (
    DEFAULT_OPERATOR_SOCKET_PATH,
    ENV_OPERATOR_SOCKET_PATH,
    MAX_IPC_MESSAGE_BYTES,
    PROTOCOL_VERSION,
)
from .enums import MotionDirection


class OperatorIpcClient:
    """Synchronous client connected to OperatorIpcServer over Unix socket."""

    def __init__(self, socket_path: str | None = None) -> None:
        if socket_path is None:
            socket_path = os.environ.get(
                ENV_OPERATOR_SOCKET_PATH, DEFAULT_OPERATOR_SOCKET_PATH
            )
        self.socket_path = socket_path
        self._sock: socket.socket | None = None
        self._buf = ""
        self._lock = threading.RLock()
        self._challenge_generations: dict[str, int] = {}
        self.last_intent_result: dict[str, Any] = {}

    def connect(self, timeout_sec: float = 3.0) -> None:
        """Connect to operator agent Unix socket."""
        with self._lock:
            if self._sock is not None:
                return

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_sec)
            try:
                sock.connect(self.socket_path)
                self._sock = sock
                self._buf = ""
            except Exception as exc:
                sock.close()
                raise ConnectionError(
                    f"Failed to connect to operator agent at '{self.socket_path}': {exc}"
                ) from exc

    def close(self) -> None:
        """Close client connection."""
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                self._buf = ""

    def __enter__(self) -> OperatorIpcClient:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _send_request(
        self, req: dict[str, Any], timeout_sec: float = 3.0
    ) -> dict[str, Any]:
        """Send request and wait for newline-delimited JSON response."""
        with self._lock:
            if self._sock is None:
                self.connect(timeout_sec=timeout_sec)
            assert self._sock is not None

            self._sock.settimeout(timeout_sec)
            req = {"protocol_version": PROTOCOL_VERSION, **req}
            payload = json.dumps(req).encode("utf-8") + b"\n"
            if len(payload) > MAX_IPC_MESSAGE_BYTES:
                raise ValueError(
                    f"Request payload exceeds {MAX_IPC_MESSAGE_BYTES} bytes cap"
                )

            try:
                self._sock.sendall(payload)

                # Read until newline
                start_time = time.monotonic()
                while "\n" not in self._buf:
                    elapsed = time.monotonic() - start_time
                    if elapsed >= timeout_sec:
                        raise TimeoutError(
                            f"Timed out waiting for IPC response after {timeout_sec:.2f}s"
                        )
                    self._sock.settimeout(max(0.1, timeout_sec - elapsed))
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise ConnectionResetError(
                            "Connection closed by operator agent"
                        )
                    self._buf += chunk.decode("utf-8", errors="replace")

                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                return json.loads(line)
            except Exception:
                # On error or timeout, reset socket and buffer to prevent stream desync
                self.close()
                raise

    def get_status(self, timeout_sec: float = 3.0) -> dict[str, Any]:
        """Query live operator and robot status."""
        return self._send_request({"action": "status"}, timeout_sec=timeout_sec)

    def get_version(self, timeout_sec: float = 3.0) -> dict[str, Any]:
        """Query protocol and release version information."""
        return self._send_request({"action": "version"}, timeout_sec=timeout_sec)

    def acquire(
        self,
        operator_id: str,
        request_id: str | None = None,
        max_linear_speed: float | None = None,
        max_angular_speed: float | None = None,
        timeout_sec: float = 3.0,
    ) -> tuple[bool, int | None, str | None, str | None]:
        """
        Acquire exclusive operator control authority with optional operating speed caps.

        Requested speeds are ceilings: query the agent caps before acquisition
        and lower requests to those caps, never increase a requested speed.
        Missing/invalid cap telemetry fails before ownership or motion. Invalid
        speed values raise ValueError. The server revalidates the request.
        Returns (success, epoch, error_code, message).
        """
        if request_id is None:
            request_id = f"acq-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "action": "acquire",
            "operator_id": operator_id,
            "request_id": request_id,
        }
        if max_linear_speed is not None or max_angular_speed is not None:
            limits = self.get_status(timeout_sec=timeout_sec)["limits"]
            for name, requested, cap_name in (
                ("max_linear_speed", max_linear_speed, "configured_linear_speed_cap"),
                (
                    "max_angular_speed",
                    max_angular_speed,
                    "configured_angular_speed_cap",
                ),
            ):
                if requested is None:
                    continue
                requested = float(requested)
                cap = float(limits[cap_name])
                if not math.isfinite(requested) or requested <= 0:
                    raise ValueError(f"{name} must be finite and positive")
                if not math.isfinite(cap) or cap <= 0:
                    raise ValueError(f"Agent supplied invalid {cap_name}")
                payload[name] = min(requested, cap)
        res = self._send_request(
            payload,
            timeout_sec=timeout_sec,
        )
        return (
            bool(res.get("success", False)),
            res.get("epoch"),
            res.get("error"),
            res.get("message"),
        )

    def release(
        self,
        epoch: int,
        request_id: str | None = None,
        timeout_sec: float = 3.0,
    ) -> tuple[bool, str | None, str | None]:
        """
        Relinquish operator authority and disarm.

        Returns (success, error_code, message).
        """
        if request_id is None:
            request_id = f"rel-{uuid.uuid4().hex[:8]}"
        res = self._send_request(
            {"action": "release", "epoch": epoch, "request_id": request_id},
            timeout_sec=timeout_sec,
        )
        return bool(res.get("success", False)), res.get("error"), res.get("message")

    def arm(
        self,
        epoch: int,
        request_id: str | None = None,
        timeout_sec: float = 5.0,
    ) -> tuple[bool, str | None, str | None]:
        """
        Explicitly arm the robot chassis controller.

        Returns (success, error_code, message).
        """
        if request_id is None:
            request_id = f"arm-{uuid.uuid4().hex[:8]}"
        res = self._send_request(
            {
                "action": "arm",
                "epoch": epoch,
                "request_id": request_id,
            },
            timeout_sec=timeout_sec,
        )
        return bool(res.get("success", False)), res.get("error"), res.get("message")

    def disarm(
        self,
        epoch: int | None = None,
        request_id: str | None = None,
        timeout_sec: float = 3.0,
    ) -> tuple[bool, str | None]:
        """
        Disarm the robot guard.

        Returns (success, message).
        """
        if request_id is None:
            request_id = f"disarm-{uuid.uuid4().hex[:8]}"
        req: dict[str, Any] = {"action": "disarm", "request_id": request_id}
        if epoch is not None:
            req["epoch"] = epoch
        res = self._send_request(req, timeout_sec=timeout_sec)
        return bool(res.get("success", False)), res.get("message")

    def stop(
        self,
        request_id: str | None = None,
        epoch: int | None = None,
        timeout_sec: float = 3.0,
    ) -> tuple[bool, str | None]:
        """
        Request immediate stop and disarm, independent of control ownership.

        Returns (success, message).
        """
        if request_id is None:
            request_id = f"stop-{uuid.uuid4().hex[:8]}"
        req: dict[str, Any] = {"action": "stop", "request_id": request_id}
        if epoch is not None:
            req["epoch"] = epoch
        res = self._send_request(req, timeout_sec=timeout_sec)
        return bool(res.get("success", False)), res.get("message")

    def request_challenge(self, epoch: int, timeout_sec: float = 1.0) -> dict[str, Any]:
        """Request single-use cryptographic challenge for motion lease renewal."""
        result = self._send_request(
            {"action": "challenge", "epoch": epoch}, timeout_sec=timeout_sec
        )
        if result.get("success"):
            self._challenge_generations[result["token"]] = result["input_generation"]
            if len(self._challenge_generations) > 64:
                self._challenge_generations.pop(next(iter(self._challenge_generations)))
        return result

    def submit_intent(
        self,
        token: str,
        epoch: int,
        sequence: int,
        direction: MotionDirection | str,
        client_timestamp_ms: int | None = None,
        timeout_sec: float = 1.0,
        input_generation: int | None = None,
    ) -> tuple[bool, str | None, str | None]:
        """
        Submit motion direction intent in response to an active challenge.

        Returns (success, active_direction, error_code).
        """
        dir_val = (
            direction.value
            if isinstance(direction, MotionDirection)
            else str(direction)
        )
        req = {
            "action": "intent",
            "response": {
                "input_generation": input_generation
                if input_generation is not None
                else self._challenge_generations.get(token, -1),
                "token": token,
                "epoch": epoch,
                "sequence": sequence,
                "direction": dir_val,
                "client_timestamp_ms": client_timestamp_ms,
            },
        }
        res = self._send_request(req, timeout_sec=timeout_sec)
        self.last_intent_result = res
        return (
            bool(res.get("success", False)),
            res.get("direction"),
            res.get("error"),
        )

    def run_motion_burst(
        self,
        epoch: int,
        direction: str,
        linear_x: float = 0.2,
        angular_z: float = 0.0,
        duration_sec: float = 1.0,
        rate_hz: float = 20.0,
        timeout_sec: float = 10.0,
    ) -> tuple[bool, str | None]:
        """
        Execute a bounded, self-terminating motion burst through deadline-checked
        challenge-intent renewals against the operator agent.
        """
        if duration_sec <= 0.0 or duration_sec > 5.0:
            return (
                False,
                f"duration_sec must be between 0.0 and 5.0s, got {duration_sec}",
            )
        if rate_hz <= 0.0 or rate_hz > 100.0:
            return False, f"rate_hz must be between 0.0 and 100.0 Hz, got {rate_hz}"

        interval = 1.0 / rate_hz
        start_time = time.monotonic()
        seq = 0
        try:
            while time.monotonic() - start_time < duration_sec:
                c = self.request_challenge(epoch, timeout_sec=0.5)
                if not c or "token" not in c or c.get("recovery_required"):
                    return False, "Motion burst interrupted; explicit Stop/Arm required"
                seq += 1
                ok, cur_dir, err = self.submit_intent(
                    c["token"], epoch, seq, direction, timeout_sec=0.5
                )
                if not ok:
                    return False, f"Motion intent rejected: {err}"
                time.sleep(interval)
        finally:
            try:
                self.stop(timeout_sec=0.5)
            except Exception:
                pass

        return True, "Burst completed successfully"

    def get_observations(
        self, since_mono_ns: int | None = None, timeout_sec: float = 3.0
    ) -> list[dict[str, Any]]:
        """Retrieve delivery observations recorded by the agent."""
        req: dict[str, Any] = {"action": "get_observations"}
        if since_mono_ns is not None:
            req["since_mono_ns"] = since_mono_ns
        res = self._send_request(req, timeout_sec=timeout_sec)
        return list(res.get("observations", []))

    def reset_observations(self, timeout_sec: float = 3.0) -> int:
        """Reset delivery observation buffer and return monotonic timestamp."""
        res = self._send_request(
            {"action": "reset_observations"}, timeout_sec=timeout_sec
        )
        return int(res.get("reset_monotonic_ns", time.monotonic_ns()))
