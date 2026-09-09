"""
Process and heartbeat supervisor node for Ubuntu Tank.

Listens on separate, dedicated channels for guard and bridge (inherited pipes or
dedicated UNIX domain sockets with SO_PASSCRED peer credential checking), AND-gates
their health, and forwards watchdog pings to systemd.
"""

import os
import select
import signal
import socket
import sys
import time

from ubuntu_tank_supervisor.heartbeat import parse_timestamp, extract_credentials
from ubuntu_tank_supervisor.supervisor import Supervisor


def main():
    guard_sock_path = os.environ.get('UBUNTU_TANK_GUARD_SOCK', '/run/ubuntu_tank/guard_heartbeat.sock')
    bridge_sock_path = os.environ.get('UBUNTU_TANK_BRIDGE_SOCK', '/run/ubuntu_tank/bridge_heartbeat.sock')
    guard_deadline = float(os.environ.get('UBUNTU_TANK_GUARD_DEADLINE', '1.0'))
    bridge_deadline = float(os.environ.get('UBUNTU_TANK_BRIDGE_DEADLINE', '1.0'))
    expected_uid = os.getuid()

    # Optional expected PIDs for socket authentication
    expected_guard_pid_str = os.environ.get('UBUNTU_TANK_GUARD_PID')
    expected_bridge_pid_str = os.environ.get('UBUNTU_TANK_BRIDGE_PID')
    guard_pid_file = os.environ.get('UBUNTU_TANK_GUARD_PID_FILE', '/run/ubuntu_tank/guard.pid')
    bridge_pid_file = os.environ.get('UBUNTU_TANK_BRIDGE_PID_FILE', '/run/ubuntu_tank/bridge.pid')

    expected_guard_pid = int(expected_guard_pid_str) if expected_guard_pid_str and expected_guard_pid_str.isdigit() else None
    expected_bridge_pid = int(expected_bridge_pid_str) if expected_bridge_pid_str and expected_bridge_pid_str.isdigit() else None

    # Check for PID files if env var not directly provided
    if expected_guard_pid is None and os.path.exists(guard_pid_file):
        try:
            with open(guard_pid_file, 'r', encoding='utf-8') as pf:
                c = pf.read().strip()
                if c.isdigit():
                    expected_guard_pid = int(c)
        except Exception:
            pass

    if expected_bridge_pid is None and os.path.exists(bridge_pid_file):
        try:
            with open(bridge_pid_file, 'r', encoding='utf-8') as pf:
                c = pf.read().strip()
                if c.isdigit():
                    expected_bridge_pid = int(c)
        except Exception:
            pass

    # Optional inherited pipe FDs (support both naming conventions)
    guard_pipe_fd_str = os.environ.get('UBUNTU_TANK_GUARD_PIPE_FD') or os.environ.get('UBUNTU_TANK_GUARD_HEARTBEAT_FD')
    bridge_pipe_fd_str = os.environ.get('UBUNTU_TANK_BRIDGE_PIPE_FD') or os.environ.get('UBUNTU_TANK_BRIDGE_HEARTBEAT_FD')

    guard_pipe_fd = int(guard_pipe_fd_str) if guard_pipe_fd_str and guard_pipe_fd_str.isdigit() else None
    bridge_pipe_fd = int(bridge_pipe_fd_str) if bridge_pipe_fd_str and bridge_pipe_fd_str.isdigit() else None

    sockets_to_close = []
    poll_descriptors = []

    # Setup dedicated guard socket if pipe not provided
    guard_sock = None
    if guard_pipe_fd is None:
        try:
            os.makedirs(os.path.dirname(guard_sock_path), exist_ok=True)
            if os.path.exists(guard_sock_path):
                os.unlink(guard_sock_path)
            guard_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            guard_sock.bind(guard_sock_path)
            guard_sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            os.chmod(guard_sock_path, 0o660)
            sockets_to_close.append((guard_sock, guard_sock_path))
            poll_descriptors.append(guard_sock)
        except Exception as e:
            sys.stderr.write(f"[Supervisor] Could not bind guard socket {guard_sock_path}: {e}\n")
    else:
        poll_descriptors.append(guard_pipe_fd)

    # Setup dedicated bridge socket if pipe not provided
    bridge_sock = None
    if bridge_pipe_fd is None:
        try:
            os.makedirs(os.path.dirname(bridge_sock_path), exist_ok=True)
            if os.path.exists(bridge_sock_path):
                os.unlink(bridge_sock_path)
            bridge_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            bridge_sock.bind(bridge_sock_path)
            bridge_sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            os.chmod(bridge_sock_path, 0o660)
            sockets_to_close.append((bridge_sock, bridge_sock_path))
            poll_descriptors.append(bridge_sock)
        except Exception as e:
            sys.stderr.write(f"[Supervisor] Could not bind bridge socket {bridge_sock_path}: {e}\n")
    else:
        poll_descriptors.append(bridge_pipe_fd)

    supervisor = Supervisor(
        guard_deadline_sec=guard_deadline,
        bridge_deadline_sec=bridge_deadline,
        expected_guard_pid=expected_guard_pid,
        expected_bridge_pid=expected_bridge_pid,
        expected_uid=expected_uid
    )

    running = True

    def handle_sig(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    supervisor.send_systemd_notification("READY=1")

    last_check = time.monotonic()
    check_interval = 0.200

    try:
        while running:
            rlist, _, _ = select.select(poll_descriptors, [], [], check_interval)
            now_mono = time.monotonic()

            for desc in rlist:
                if guard_sock is not None and desc is guard_sock:
                    try:
                        msg, ancdata, _, _ = guard_sock.recvmsg(256, 256)
                        pid, uid, _ = extract_credentials(ancdata)
                        ts = parse_timestamp(msg)
                        if ts is not None:
                            if supervisor.expected_guard_pid is None and os.path.exists(guard_pid_file):
                                try:
                                    with open(guard_pid_file, 'r', encoding='utf-8') as pf:
                                        c = pf.read().strip()
                                        if c.isdigit():
                                            supervisor.set_expected_pid('guard', int(c))
                                except Exception:
                                    pass
                            ok, reason = supervisor.record_heartbeat(
                                'guard', now_mono, peer_pid=pid, peer_uid=uid, is_trusted_channel=False
                            )
                            if not ok:
                                sys.stderr.write(f"[Supervisor] Rejected guard socket heartbeat: {reason}\n")
                                sys.stderr.flush()
                    except Exception:
                        pass
                elif bridge_sock is not None and desc is bridge_sock:
                    try:
                        msg, ancdata, _, _ = bridge_sock.recvmsg(256, 256)
                        pid, uid, _ = extract_credentials(ancdata)
                        ts = parse_timestamp(msg)
                        if ts is not None:
                            if supervisor.expected_bridge_pid is None and os.path.exists(bridge_pid_file):
                                try:
                                    with open(bridge_pid_file, 'r', encoding='utf-8') as pf:
                                        c = pf.read().strip()
                                        if c.isdigit():
                                            supervisor.set_expected_pid('bridge', int(c))
                                except Exception:
                                    pass
                            ok, reason = supervisor.record_heartbeat(
                                'bridge', now_mono, peer_pid=pid, peer_uid=uid, is_trusted_channel=False
                            )
                            if not ok:
                                sys.stderr.write(f"[Supervisor] Rejected bridge socket heartbeat: {reason}\n")
                                sys.stderr.flush()
                    except Exception:
                        pass
                elif guard_pipe_fd is not None and desc == guard_pipe_fd:
                    try:
                        data = os.read(guard_pipe_fd, 256)
                        ts = parse_timestamp(data)
                        if ts is not None:
                            supervisor.record_heartbeat('guard', now_mono, is_trusted_channel=True)
                    except Exception:
                        pass
                elif bridge_pipe_fd is not None and desc == bridge_pipe_fd:
                    try:
                        data = os.read(bridge_pipe_fd, 256)
                        ts = parse_timestamp(data)
                        if ts is not None:
                            supervisor.record_heartbeat('bridge', now_mono, is_trusted_channel=True)
                    except Exception:
                        pass

            if now_mono - last_check >= check_interval:
                last_check = now_mono
                healthy, reason = supervisor.check_health(now_mono)
                if healthy:
                    supervisor.send_systemd_notification("WATCHDOG=1")
                else:
                    sys.stderr.write(f"[Supervisor] Health check failed: {reason}\n")
                    sys.stderr.flush()

    finally:
        for sock, path in sockets_to_close:
            try:
                sock.close()
                if os.path.exists(path):
                    os.unlink(path)
            except Exception:
                pass


if __name__ == '__main__':
    main()
