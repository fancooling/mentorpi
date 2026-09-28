"""
Configuration models and validation for MentorPi Pi 5 Web Control.

Validates web server and operator agent configuration parameters at startup,
preventing unsafe wildcards, excessive speeds, or unvalidated addresses.
"""

import ipaddress
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from .constants import (
    CHALLENGE_INTERVAL_SEC,
    DEFAULT_WEB_ANGULAR_SPEED,
    DEFAULT_WEB_LINEAR_SPEED,
    IDLE_TIMEOUT_SEC,
    LEASE_DURATION_SEC,
    MAX_CONTINUOUS_HOLD_SEC,
    MAX_PERMISSIBLE_ANGULAR_SPEED,
    MAX_PERMISSIBLE_LINEAR_SPEED,
)


@dataclass(frozen=True)
class WebControlConfig:
    """Configuration settings for MentorPi Web Control and Operator services."""

    listen_address: str = "127.0.0.1"
    port: int = 8443
    tls_cert_path: str | None = "/var/opt/ubuntu_tank/web/certs/server.crt"
    tls_key_path: str | None = "/var/opt/ubuntu_tank/web/certs/server.key"
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "https://127.0.0.1:8443",
            "https://localhost:8443",
        ]
    )
    linear_speed_cap: float = DEFAULT_WEB_LINEAR_SPEED
    angular_speed_cap: float = DEFAULT_WEB_ANGULAR_SPEED
    lease_duration_sec: float = LEASE_DURATION_SEC
    challenge_interval_sec: float = CHALLENGE_INTERVAL_SEC
    idle_timeout_sec: float = IDLE_TIMEOUT_SEC
    max_hold_duration_sec: float = MAX_CONTINUOUS_HOLD_SEC
    operator_socket_path: str = "/run/ubuntu_tank/operator.sock"
    lifecycle_socket_path: str = "/run/ubuntu_tank/lifecycle.sock"

    def validate(self) -> None:
        """Validate all fields against security and safety bounds."""
        # 1. IP address validation
        try:
            ipaddress.ip_address(self.listen_address)
        except ValueError:
            if self.listen_address not in ("localhost", "0.0.0.0"):
                raise ValueError(
                    f"Invalid listen_address: '{self.listen_address}' must be a valid IP address"
                )

        # 2. Port bounds check
        if not isinstance(self.port, int) or self.port < 1 or self.port > 65535:
            raise ValueError(f"Invalid port: {self.port} must be between 1 and 65535")

        # 3. Allowed origins check: prohibit wildcard '*'
        if not self.allowed_origins:
            raise ValueError("allowed_origins must not be empty")
        for origin in self.allowed_origins:
            if origin == "*":
                raise ValueError(
                    "allowed_origins must NOT contain wildcard '*' for security"
                )
            if not origin.startswith(("http://", "https://")):
                raise ValueError(
                    f"Invalid origin '{origin}': must start with http:// or https://"
                )

        # 4. Speed caps check
        if (
            self.linear_speed_cap <= 0.0
            or self.linear_speed_cap > MAX_PERMISSIBLE_LINEAR_SPEED
        ):
            raise ValueError(
                f"linear_speed_cap {self.linear_speed_cap} exceeds safety limits"
                f" (0.0 < speed <= {MAX_PERMISSIBLE_LINEAR_SPEED} m/s)"
            )
        if (
            self.angular_speed_cap <= 0.0
            or self.angular_speed_cap > MAX_PERMISSIBLE_ANGULAR_SPEED
        ):
            raise ValueError(
                f"angular_speed_cap {self.angular_speed_cap} exceeds safety limits"
                f" (0.0 < speed <= {MAX_PERMISSIBLE_ANGULAR_SPEED} rad/s)"
            )

        # Reject coercions and NaN before comparisons, which NaN can bypass.
        for name in ("lease_duration_sec", "challenge_interval_sec"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        # 5. Timing parameter bounds check
        if self.lease_duration_sec < 0.050 or self.lease_duration_sec > 1.000:
            raise ValueError(
                f"lease_duration_sec {self.lease_duration_sec} outside valid range"
                " (0.050 to 1.000 s)"
            )
        if (
            self.challenge_interval_sec < 0.010
            or self.challenge_interval_sec >= self.lease_duration_sec
        ):
            raise ValueError(
                f"challenge_interval_sec {self.challenge_interval_sec} must be <"
                f" lease_duration_sec ({self.lease_duration_sec}) and >= 0.010 s"
            )
        if self.idle_timeout_sec < 1.0 or self.idle_timeout_sec > 300.0:
            raise ValueError(
                f"idle_timeout_sec {self.idle_timeout_sec} outside valid range (1.0 to"
                " 300.0 s)"
            )
        if self.max_hold_duration_sec < 0.5 or self.max_hold_duration_sec > 10.0:
            raise ValueError(
                f"max_hold_duration_sec {self.max_hold_duration_sec} outside valid"
                " range (0.5 to 10.0 s)"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebControlConfig":
        cfg = cls(
            listen_address=data.get("listen_address", "127.0.0.1"),
            port=data.get("port", 8443),
            tls_cert_path=data.get(
                "tls_cert_path", "/etc/opt/ubuntu_tank/web/certs/server.crt"
            ),
            tls_key_path=data.get(
                "tls_key_path", "/etc/opt/ubuntu_tank/web/certs/server.key"
            ),
            allowed_origins=list(
                data.get(
                    "allowed_origins",
                    ["https://127.0.0.1:8443", "https://localhost:8443"],
                )
            ),
            linear_speed_cap=data.get("linear_speed_cap", DEFAULT_WEB_LINEAR_SPEED),
            angular_speed_cap=data.get("angular_speed_cap", DEFAULT_WEB_ANGULAR_SPEED),
            lease_duration_sec=data.get("lease_duration_sec", LEASE_DURATION_SEC),
            challenge_interval_sec=data.get(
                "challenge_interval_sec", CHALLENGE_INTERVAL_SEC
            ),
            idle_timeout_sec=data.get("idle_timeout_sec", IDLE_TIMEOUT_SEC),
            max_hold_duration_sec=data.get(
                "max_hold_duration_sec", MAX_CONTINUOUS_HOLD_SEC
            ),
            operator_socket_path=data.get(
                "operator_socket_path", "/run/ubuntu_tank/operator.sock"
            ),
            lifecycle_socket_path=data.get(
                "lifecycle_socket_path", "/run/ubuntu_tank/lifecycle.sock"
            ),
        )
        cfg.validate()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
