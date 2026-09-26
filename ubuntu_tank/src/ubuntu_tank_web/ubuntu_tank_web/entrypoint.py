"""
CLI entrypoint for running the MentorPi Pi 5 Web Control Service.

Invoked directly or by bin/mentorpi-tank-web / systemd service.
Loads configuration, validates externally provisioned TLS certificates, and runs
the single-worker Uvicorn ASGI server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import uvicorn
import yaml
from ubuntu_tank_protocol.config import WebControlConfig

from .app import create_app
from .tls import validate_tls_certificate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mentorpi-tank-web")


def load_config_from_yaml(config_path: str) -> WebControlConfig:
    """Load and validate WebControlConfig from a YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise TypeError("Web configuration must be a YAML mapping")
    return WebControlConfig.from_dict(data)


def main(argv: list[str] | None = None) -> int:
    """Main execution entrypoint."""
    parser = argparse.ArgumentParser(description="MentorPi Tank Web Control Service")
    parser.add_argument(
        "--config",
        type=str,
        default="/etc/opt/ubuntu_tank/web/web.yaml",
        help="Path to YAML configuration file",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Listen address override",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Listen port override",
    )
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="Run without TLS (plain HTTP, testing only)",
    )
    parser.add_argument(
        "--static-dir",
        type=str,
        default=None,
        help="Path to compiled frontend static assets",
    )
    parser.add_argument(
        "--export-openapi",
        type=str,
        default=None,
        help="Export OpenAPI 3.0 specification to JSON file and exit",
    )
    args = parser.parse_args(argv)

    # Artifact generation does not require host-specific configuration or TLS.
    cfg = (
        WebControlConfig()
        if args.export_openapi
        else load_config_from_yaml(args.config)
    )

    if args.export_openapi:
        app = create_app(config=cfg)
        spec = app.openapi()
        out_dir = os.path.dirname(os.path.abspath(args.export_openapi))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.export_openapi, "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2)
        logger.info("Exported OpenAPI spec to %s", args.export_openapi)
        return 0
    host = args.host if args.host is not None else cfg.listen_address
    port = args.port if args.port is not None else cfg.port

    ssl_cert: str | None = None
    ssl_key: str | None = None

    if not args.no_tls:
        if not cfg.tls_cert_path or not cfg.tls_key_path:
            raise ValueError("TLS certificate and key paths are required")
        cert_p, key_p = validate_tls_certificate(
            cert_path=cfg.tls_cert_path,
            key_path=cfg.tls_key_path,
        )
        ssl_cert = cert_p
        ssl_key = key_p
        logger.info("TLS termination active using cert: %s", ssl_cert)

    app = create_app(config=cfg, static_dir=args.static_dir)

    protocol = "https" if ssl_cert else "http"
    logger.info(
        "Starting MentorPi Tank Web Service at %s://%s:%d", protocol, host, port
    )

    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=ssl_cert,
        ssl_keyfile=ssl_key,
        workers=1,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
