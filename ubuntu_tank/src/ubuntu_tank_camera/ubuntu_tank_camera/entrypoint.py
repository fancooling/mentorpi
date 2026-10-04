"""Entrypoint for the runtime camera worker daemon.

Runs as an independent supervisor-managed program inside the runtime container,
owning /dev/video0 and exposing /run/ubuntu_tank/camera.sock.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from .ipc_server import CameraIpcServer
from .v4l2_capture import V4L2CameraCapture

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("ubuntu_tank_camera")


def main() -> int:
    """Run the camera worker daemon until terminated."""
    logger.info("Initializing ubuntu_tank_camera worker...")

    capture = V4L2CameraCapture()
    server = CameraIpcServer(capture=capture)

    running = True

    def _shutdown(signum: int, frame: object) -> None:
        nonlocal running
        logger.info("Received signal %d; shutting down camera worker...", signum)
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    capture.start()
    server.start()

    logger.info("Camera worker running. Listening on %s", server.socket_path)

    try:
        while running:
            time.sleep(0.5)
    finally:
        logger.info("Stopping camera worker...")
        server.stop()
        capture.stop()
        logger.info("Camera worker stopped cleanly.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
