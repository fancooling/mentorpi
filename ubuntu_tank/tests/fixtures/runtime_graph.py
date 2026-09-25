"""Motor-free ROS graph double: real child processes send credentialed heartbeats."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

if len(sys.argv) > 1:
    role = sys.argv[1]
    Path(os.environ[f"UBUNTU_TANK_{role}_PID_FILE"]).write_text(str(os.getpid()))
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    while True:
        channel.sendto(
            str(time.monotonic()).encode(), os.environ[f"UBUNTU_TANK_{role}_SOCK"]
        )
        time.sleep(0.03)
else:
    gate = os.environ.get("C2_GRAPH_GATE")
    while gate and not Path(gate).exists():
        time.sleep(0.02)
    children = [
        subprocess.Popen([sys.executable, __file__, role])
        for role in ("GUARD", "BRIDGE")
    ]
    # Simulate a launch parent that can hang independently of its children.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        time.sleep(1)
