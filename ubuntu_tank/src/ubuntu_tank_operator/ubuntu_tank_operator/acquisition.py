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
    """Serialize Take control and Release control while allowing Stop to cancel blocking startup."""

    def __init__(self, server: Any, lifecycle: Any, bind_timeout: float = 5.0) -> None:
        self.server = server
        self.lifecycle = lifecycle
        self.bind_timeout = bind_timeout
        self.operations: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.pending: str | None = None
        self.pending_release: str | None = None
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
            if (
                self.pending
                or self.pending_release
                or self.server._owner_conn is not None
                or self.server.state_machine.release_progress
                not in (None, "shutdown_failed")
            ):
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
                if op.get("type") == "release":
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

    def begin_release(
        self, sock: Any, req: dict[str, Any], client: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Begin an owner-scoped release operation including controller shutdown."""
        with self.server._lock:
            op_id = req.get("operation_id")
            cancelling_setup = False
            if op_id is not None:
                for prior in self.operations.values():
                    if (
                        prior.get("type") == "release"
                        and prior["socket"] is sock
                        and prior["request"].get("operation_id") == op_id
                        and prior["status"] != "failed"
                    ):
                        return self.result(sock, prior["operation_id"])
                existing = self.operations.get(op_id)
                if existing is not None:
                    if existing["socket"] is not sock:
                        return {
                            "success": False,
                            "status": "failed",
                            "error": "NOT_OWNER",
                        }
                    if existing.get("type") == "release":
                        return self.result(sock, op_id)
                    if existing.get("status") == "pending":
                        self.cancel(sock=sock, stop_service=True)
                        cancelling_setup = True
                    # If existing is a completed acquisition, fall through to full release.
                elif self.server._owner_conn is not sock:
                    return {"success": False, "status": "failed", "error": "NOT_OWNER"}

            request_id = req.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                request_id = f"rel-{uuid.uuid4().hex[:8]}"

            for op in self.operations.values():
                if (
                    op.get("type") == "release"
                    and op["socket"] is sock
                    and op["request_id"] == request_id
                ):
                    if op["request"] != req:
                        return {
                            "success": False,
                            "status": "failed",
                            "error": "INVALID_PAYLOAD",
                            "message": "Request ID reused with different input",
                        }
                    return self.result(sock, op["operation_id"])

            retrying_setup_release = (
                self.server._owner_conn is None
                and self.server.state_machine.release_progress == "shutdown_failed"
                and any(
                    op.get("type") == "release" and op["socket"] is sock
                    for op in self.operations.values()
                )
            )
            if (
                self.server._owner_conn is not sock
                and not cancelling_setup
                and not retrying_setup_release
            ):
                return {
                    "success": False,
                    "status": "failed",
                    "error": "NOT_OWNER",
                    "message": "Caller does not hold operator authority",
                }

            epoch = req.get("epoch")
            if epoch is not None and epoch != self.server.state_machine.epoch:
                return {
                    "success": False,
                    "status": "failed",
                    "error": "INVALID_EPOCH",
                    "message": f"Mismatched epoch {epoch} (current is {self.server.state_machine.epoch})",
                }

            if self.pending_release is not None:
                return {
                    "success": False,
                    "status": "failed",
                    "error": "DEPLOYMENT_BUSY",
                    "message": "Release already in progress",
                }

            now_ns = time.monotonic_ns()
            owner_id = (
                self.server.state_machine.owner_id
                or self.server._owner_id
                or req.get("operator_id")
                or ""
            )

            # Clear bind deadlines and cancel pending setups on this socket without wiping ownership
            for op_item in self.operations.values():
                if op_item.get("socket") is sock:
                    op_item["bind_deadline"] = None
                    if (
                        op_item.get("status") == "pending"
                        and op_item.get("type") != "release"
                    ):
                        op_item.update(
                            cancelled=True,
                            status="failed",
                            error="CANCELLED",
                        )

            if self.lifecycle is None:
                ok, err, msg = self.server.state_machine.release(
                    owner_id, epoch or self.server.state_machine.epoch, now_ns
                )
                if ok:
                    self.server.state_machine.last_released_session_id = owner_id
                    self.server.state_machine.last_release_reason = (
                        req.get("reason") or "EXPLICIT_RELEASE"
                    )
                    self.server.state_machine.status_revision += 1
                    self.server._owner_conn = None
                    self.server._owner_id = None
                    self.server._owner_pid = None
                return {
                    "success": ok,
                    "status": "completed" if ok else "failed",
                    "error": err.value if err else None,
                    "message": msg or "Ownership released",
                }

            self.server.state_machine.stop(now_ns, requester_id=self.server._owner_id)

            while len(self.operations) >= 100:
                self.operations.popitem(last=False)
            operation_id = uuid.uuid4().hex
            op = {
                "operation_id": operation_id,
                "type": "release",
                "status": "pending",
                "success": True,
                "socket": sock,
                "request_id": request_id,
                "request": dict(req),
                "owner_id": owner_id,
                "generation": self.generation,
                "error": None,
                "message": None,
                "cancelled": False,
            }
            self.operations[operation_id] = op
            self.pending_release = operation_id
            self.server.state_machine.release_progress = "stopping_controller"
            self.server.state_machine.last_released_session_id = owner_id
            self.server.state_machine.status_revision += 1

            threading.Thread(
                target=self._run_release,
                args=(op, req.get("reason") or "EXPLICIT_RELEASE"),
                daemon=True,
            ).start()
            return self.result(sock, operation_id)

    def begin_idle_release(self, sock: Any, owner_id: str) -> None:
        """Trigger automatic release due to ownership inactivity."""
        with self.server._lock:
            if self.pending_release is not None:
                return
            if self.lifecycle is None:
                return
            operation_id = uuid.uuid4().hex
            req = {
                "request_id": f"idle-{uuid.uuid4().hex[:8]}",
                "reason": "CONTROL_IDLE_TIMEOUT",
            }
            op = {
                "operation_id": operation_id,
                "type": "release",
                "status": "pending",
                "success": True,
                "socket": sock,
                "request_id": req["request_id"],
                "request": req,
                "owner_id": owner_id,
                "generation": self.generation,
                "error": None,
                "message": None,
                "cancelled": False,
            }
            while len(self.operations) >= 100:
                self.operations.popitem(last=False)
            self.operations[operation_id] = op
            self.pending_release = operation_id
            self.server.state_machine.release_progress = "stopping_controller"
            self.server.state_machine.last_released_session_id = owner_id
            self.server.state_machine.status_revision += 1

            threading.Thread(
                target=self._run_release,
                args=(op, "CONTROL_IDLE_TIMEOUT"),
                daemon=True,
            ).start()

    def _run_release(
        self, op: dict[str, Any], reason: str = "EXPLICIT_RELEASE"
    ) -> None:
        try:
            if self.lifecycle is None:
                raise RuntimeError("Controller status unavailable")
            # A cancelled startup may still be inside its lifecycle call. Wait
            # for its cleanup before confirming release, without blocking IPC.
            deadline = time.monotonic() + 20.0
            while True:
                with self.server._lock:
                    pending = self.operations.get(self.pending)
                    waiting = pending is not None and pending["socket"] is op["socket"]
                if not waiting:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Cancelled controller startup has not finished")
                time.sleep(0.02)
            self._confirm_shutdown()

            with self.server._lock:
                now_ns = time.monotonic_ns()
                owner_id = (
                    op.get("owner_id")
                    or self.server.state_machine.owner_id
                    or self.server._owner_id
                    or ""
                )
                self.server.state_machine.release(
                    owner_id,
                    self.server.state_machine.epoch,
                    now_ns,
                )
                # A live web socket may outlast automatic release. Retire its
                # grant so the relay can discard the old client on the next Take.
                for grant in self.operations.values():
                    if (
                        grant["socket"] is op["socket"]
                        and grant.get("type") != "release"
                    ):
                        grant.update(
                            status="failed",
                            success=False,
                            error="NOT_OWNER",
                            epoch=None,
                            bind_deadline=None,
                        )
                self.server.state_machine.release_progress = None
                self.server.state_machine.last_release_reason = reason
                self.server.state_machine.last_released_session_id = owner_id
                self.server.state_machine.status_revision += 1
                self.server._owner_conn = None
                self.server._owner_id = None
                self.server._owner_pid = None
                self.pending_release = None
                op.update(
                    status="completed",
                    success=True,
                    message="Controller stopped and ownership released",
                )
        except Exception as exc:  # noqa: BLE001 - fail closed for every lifecycle worker failure
            with self.server._lock:
                self.server.state_machine.release_progress = "shutdown_failed"
                self.server.state_machine.last_release_reason = "SHUTDOWN_FAILED"
                self.server.state_machine.status_revision += 1
                self.pending_release = None
                op.update(
                    status="failed",
                    success=False,
                    error="OPERATION_FAILED",
                    message=str(exc),
                )

    def _confirm_shutdown(self) -> None:
        """Stop off the IPC thread and require observed inactive before handoff."""
        ok, _, message = self.lifecycle.stop_controller()
        if not ok:
            raise RuntimeError(message or "Failed to stop controller")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ok, state, _ = self.lifecycle.get_status()
            if ok and state == "inactive":
                return
            time.sleep(0.02)
        raise RuntimeError("Timed out waiting for controller to reach inactive state")

    def bind(self, sock: Any, operation_id: str) -> bool:
        """Confirm the browser bound before the runtime's unbound-grant deadline."""
        with self.server._lock:
            self.expire()
            op = self.operations.get(operation_id)
            if (
                not op
                or op["socket"] is not sock
                or op["status"] != "completed"
                or op.get("bind_deadline") is None
            ):
                return False
            op["bind_deadline"] = None
            return True

    def expire(self) -> None:
        """Revoke abandoned grants on the IPC loop, independently of web polling."""
        with self.server._lock:
            for op in self.operations.values():
                deadline = op.get("bind_deadline")
                if deadline is not None and time.monotonic() >= deadline:
                    op.update(
                        bind_deadline=None,
                        status="failed",
                        success=False,
                        epoch=None,
                        error="TIMEOUT",
                        message="Socket bind timed out",
                    )
                    if self.server._owner_conn is op.get("socket"):
                        self.server._handle_owner_lost("Socket bind timed out")

    def _run(self, op: dict[str, Any], client: dict[str, Any]) -> None:
        started = False
        try:
            with self.server._lock:
                recovering = (
                    self.server.state_machine.release_progress == "shutdown_failed"
                )
            if recovering:
                # The previous socket is gone. A new Take can retry cleanup, but
                # may not start/acquire until the old controller is confirmed off.
                self._confirm_shutdown()
                with self.server._lock:
                    self.server.state_machine.release_progress = None
                    self.server.state_machine.status_revision += 1
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
