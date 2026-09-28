"""Runtime-owned, cancellable controller startup and connection-bound acquisition.

Lifecycle calls run off the IPC loop. Results and deduplication are bounded and
private to the originating socket. Stop invalidates pending work before returning;
unbound grants expire without ever arming the guard.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

from ubuntu_tank_protocol.deployment import admitted


class AcquisitionCoordinator:
    """Serialize Take control while allowing Stop to cancel blocking startup."""

    def __init__(self, server: Any, lifecycle: Any, bind_timeout: float = 5.0) -> None:
        self.server = server
        self.lifecycle = lifecycle
        self.bind_timeout = bind_timeout
        self.operations: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.pending: str | None = None
        self.generation = 0

    def begin(
        self, sock: Any, req: dict[str, Any], client: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a deduplicated private operation or reserve the sole setup slot."""
        with self.server._lock:
            request_id, owner = req.get("request_id"), req.get("operator_id")
            if (
                not isinstance(request_id, str)
                or not request_id
                or not isinstance(owner, str)
                or not owner
            ):
                return {"success": False, "error": "INVALID_PAYLOAD"}
            for op in self.operations.values():
                if op["socket"] is sock and op["request_id"] == request_id:
                    if op["request"] != req:
                        return {
                            "success": False,
                            "error": "INVALID_PAYLOAD",
                            "message": "Request ID reused with different input",
                        }
                    return self.result(sock, op["operation_id"])
            if not admitted():
                return {"success": False, "error": "DEPLOYMENT_BUSY"}
            if self.pending or self.server._owner_conn is not None:
                return {"success": False, "error": "DEPLOYMENT_BUSY"}
            if self.lifecycle is None:
                return {"success": False, "error": "CONTROLLER_UNAVAILABLE"}
            while len(self.operations) >= 100:
                self.operations.popitem(last=False)
            operation_id = uuid.uuid4().hex
            op = {
                "operation_id": operation_id,
                "status": "pending",
                "success": True,
                "socket": sock,
                "request_id": request_id,
                "request": dict(req),
                "generation": self.generation,
                "epoch": None,
                "error": None,
                "bind_deadline": None,
                "cancelled": False,
                "stop_service": False,
            }
            self.operations[operation_id] = op
            self.pending = operation_id
            threading.Thread(target=self._run, args=(op, client), daemon=True).start()
            return self.result(sock, operation_id)

    def result(self, sock: Any, operation_id: str) -> dict[str, Any]:
        """Only the originating IPC connection may retrieve setup results."""
        with self.server._lock:
            op = self.operations.get(operation_id)
            if op is None or op["socket"] is not sock:
                return {"success": False, "error": "NOT_OWNER"}
            return {
                k: op.get(k)
                for k in (
                    "operation_id",
                    "status",
                    "success",
                    "epoch",
                    "error",
                    "message",
                )
            }

    def cancel(self, sock: Any = None, stop_service: bool = False) -> None:
        """Invalidate pending/unbound work globally or for one owning connection."""
        with self.server._lock:
            self.generation += 1
            for op in self.operations.values():
                if sock is not None and op["socket"] is not sock:
                    continue
                # A cancelled worker can still be delivering its startup request.
                # Controller Stop must upgrade its cleanup even after failure.
                if stop_service and self.pending == op["operation_id"]:
                    op["stop_service"] = True
                # Bound sessions are no longer setup transactions. Motion Stop
                # retains their ownership until explicit release or disconnect.
                if op["status"] == "pending" or (
                    op["status"] == "completed" and op["bind_deadline"] is not None
                ):
                    was_unbound = op["bind_deadline"] is not None
                    op.update(
                        cancelled=True,
                        stop_service=op["stop_service"] or stop_service,
                        status="failed",
                        success=False,
                        error="STALE_TRANSACTION",
                        epoch=None,
                        bind_deadline=None,
                    )
                    if was_unbound and self.server._owner_conn is op["socket"]:
                        self.server._handle_owner_lost("Acquisition cancelled")

    def bind(self, sock: Any, operation_id: str) -> bool:
        """Confirm the browser bound before the runtime's unbound-grant deadline."""
        with self.server._lock:
            self.expire()
            op = self.operations.get(operation_id)
            if (
                not op
                or op["socket"] is not sock
                or op["status"] != "completed"
                or op["bind_deadline"] is None
            ):
                return False
            op["bind_deadline"] = None
            return True

    def expire(self) -> None:
        """Revoke abandoned grants on the IPC loop, independently of web polling."""
        with self.server._lock:
            for op in self.operations.values():
                if (
                    op["bind_deadline"] is not None
                    and time.monotonic() >= op["bind_deadline"]
                ):
                    op.update(
                        bind_deadline=None,
                        status="failed",
                        success=False,
                        epoch=None,
                        error="TIMEOUT",
                        message="Socket bind timed out",
                    )
                    if self.server._owner_conn is op["socket"]:
                        self.server._handle_owner_lost("Socket bind timed out")

    def _run(self, op: dict[str, Any], client: dict[str, Any]) -> None:
        started = False
        try:
            ok, state, message = self.lifecycle.get_status()
            if not ok:
                raise RuntimeError(message or "Controller status unavailable")
            with self.server._lock:
                if op["cancelled"]:
                    return
            if state != "active":
                started = True
                ok, state, message = self.lifecycle.start_controller()
                if not ok or state != "active":
                    raise RuntimeError(message or "Controller startup failed")
            deadline = time.monotonic() + 5.0
            while True:
                with self.server._lock:
                    if (
                        op["cancelled"]
                        or op["socket"] not in self.server._clients
                        or not admitted()
                    ):
                        raise RuntimeError(
                            "Acquisition cancelled or deployment unavailable"
                        )
                    now = time.monotonic_ns()
                    sm = self.server.state_machine
                    healthy, _, _ = sm.telemetry.is_healthy(now)
                    if (
                        healthy
                        and sm.telemetry.guard_armed is False
                        and not sm.disarm_pending
                    ):
                        # Reserve remains held until this atomic ownership transfer.
                        req = op["request"]
                        ok, epoch, error, message = sm.acquire(
                            req["operator_id"],
                            now,
                            max_linear_speed=req.get("max_linear_speed"),
                            max_angular_speed=req.get("max_angular_speed"),
                        )
                        if not ok:
                            raise RuntimeError(message or str(error))
                        self.server._owner_conn = op["socket"]
                        self.server._owner_id = req["operator_id"]
                        self.server._owner_pid = client.get("pid")
                        op.update(
                            status="completed",
                            epoch=epoch,
                            bind_deadline=time.monotonic() + self.bind_timeout,
                        )
                        return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Controller readiness timed out")
                time.sleep(0.02)
        except Exception as exc:  # noqa: BLE001 - terminate any failed lifecycle worker safely
            with self.server._lock:
                op.update(
                    status="failed",
                    success=False,
                    error="OPERATION_FAILED",
                    message=str(exc),
                )
        finally:
            # Decide cleanup and worker completion atomically with cancellation.
            with self.server._lock:
                stop_service = started and (
                    op["stop_service"]
                    or (op["status"] == "failed" and not op["cancelled"])
                )
                if not stop_service and self.pending == op["operation_id"]:
                    self.pending = None
            if stop_service:
                try:
                    self.lifecycle.stop_controller()
                finally:
                    with self.server._lock:
                        if self.pending == op["operation_id"]:
                            self.pending = None
