"""Bounded local Supervisor XML-RPC operations over its private Unix socket."""

import http.client
import socket
import xmlrpc.client

from .progress import API_TIMEOUT, private_dir


class _Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(API_TIMEOUT)
        self.sock.connect(str(private_dir() / "supervisor.sock"))


class _Transport(xmlrpc.client.Transport):
    def make_connection(self, host):
        self._connection = (host, _Connection(host, timeout=API_TIMEOUT))
        return self._connection[1]


def call(method: str, *args):
    """Call Supervisor with a fresh connection so concurrent Stop never queues locally."""
    with xmlrpc.client.ServerProxy(
        "http://localhost/RPC2", transport=_Transport()
    ) as proxy:
        return getattr(proxy.supervisor, method)(*args)


def controller_operation(args: list[str]) -> tuple[int, str, str]:
    """Execute only controller start/stop/status; return code, state, and error."""
    try:
        action = args[0]
        if action in ("start", "stop"):
            try:
                call(action + "Process", "controller", False)
            except xmlrpc.client.Fault as exc:
                # Idempotent Start and Stop only; all other faults remain errors.
                if exc.faultCode != ({"start": 60, "stop": 70}[action]):
                    raise
            return 0, "", ""
        if action != "is-active":
            raise ValueError("Unsupported controller operation")
        info = call("getProcessInfo", "controller")
        state = {
            "RUNNING": "active",
            "STOPPED": "inactive",
            "EXITED": "inactive",
            "FATAL": "failed",
            "BACKOFF": "failed",
        }.get(info["statename"], "unknown")
        return 0, state, ""
    except (OSError, ValueError, KeyError, xmlrpc.client.Error) as exc:
        return -1, "unknown", str(exc)


def controller_logs(limit: int) -> list[str]:
    """Read at most 24 KiB from the fixed controller log through Supervisor."""
    text = call("readProcessStdoutLog", "controller", -24000, 0)
    return text.splitlines()[-max(1, min(200, limit)) :]
