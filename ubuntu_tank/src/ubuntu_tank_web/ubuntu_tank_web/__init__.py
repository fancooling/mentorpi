"""
ubuntu_tank_web - MentorPi Pi 5 Web Control Service.

Provides a FastAPI backend and Uvicorn server for browser-based tank control,
telemetry, and system lifecycle operations.
"""

from .lifecycle_client import LifecycleClient
from .operator_relay import OperatorRelay
from .tls import ensure_tls_certificate, generate_self_signed_cert

__all__ = [
    "create_app",
    "ensure_tls_certificate",
    "generate_self_signed_cert",
    "LifecycleClient",
    "OperatorRelay",
]


def __getattr__(name: str):
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
