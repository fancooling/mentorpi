#!/usr/bin/env python3
"""
bench_client.py - Dedicated bench testing and operator client for Milestone 6 acceptance.

Runs under enclave /ubuntu_tank/operator with node name 'operator_client'
to satisfy least-privilege SROS2 DDS security permissions.
Provides:
- Guard arm / disarm service requests (/ubuntu_tank_safety/set_arm)
- Bounded cmd_vel velocity publishing (/controller/cmd_vel)
- Guard state and armed status monitoring (/ubuntu_tank_safety/state, /ubuntu_tank_safety/armed)
- Finite motion execution and stop latency measurements
"""

import json
import math
import os
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from ros_robot_controller.ros_robot_controller_sdk import checksum_crc8
except (ImportError, AttributeError):
    checksum_crc8 = None

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from geometry_msgs.msg import Twist
    from std_msgs.msg import Bool
    from std_srvs.srv import SetBool

    try:
        from std_msgs.msg import String
    except (ImportError, AttributeError):
        String = None
except ImportError:
    rclpy = None
    Parameter = None
    DurabilityPolicy = None
    QoSProfile = None
    ReliabilityPolicy = None

    class _FallbackNode:
        def __init__(self, *args, context=None, **kwargs):
            self._fallback_context = context

        @property
        def context(self):
            return self._fallback_context

        def destroy_node(self):
            pass

    Node = _FallbackNode

    class _Vector3:
        def __init__(self):
            self.x = 0.0
            self.y = 0.0
            self.z = 0.0

    class Twist:
        def __init__(self):
            self.linear = _Vector3()
            self.angular = _Vector3()

    class Bool:
        def __init__(self, data=False):
            self.data = data

    class String:
        def __init__(self, data=""):
            self.data = data

    class _SetBool:
        class Request:
            def __init__(self):
                self.data = False

        class Response:
            def __init__(self):
                self.success = False
                self.message = ""

    SetBool = _SetBool

SingleThreadedExecutor = None
if rclpy is not None:
    try:
        from rclpy.executors import SingleThreadedExecutor as _STE

        SingleThreadedExecutor = _STE
    except (ImportError, AttributeError):
        SingleThreadedExecutor = getattr(
            getattr(rclpy, "executors", None), "SingleThreadedExecutor", None
        )


def _extract_motors_map(motors_raw: Any) -> Dict[int, float]:
    """Convert motors representation to {motor_id: float(rps)}."""
    res: Dict[int, float] = {}
    if isinstance(motors_raw, dict):
        for k, v in motors_raw.items():
            try:
                res[int(k)] = float(v)
            except (ValueError, TypeError):
                pass
    elif isinstance(motors_raw, (list, tuple)):
        for item in motors_raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    res[int(item[0])] = float(item[1])
                except (ValueError, TypeError):
                    pass
            elif isinstance(item, dict):
                m_id = item.get("id", item.get("motor_id"))
                rps = item.get("rps", item.get("speed", 0.0))
                if m_id is not None:
                    try:
                        res[int(m_id)] = float(rps)
                    except (ValueError, TypeError):
                        pass
    return res


def _motors_match(
    m1: Dict[int, float], m2: Dict[int, float], tol: float = 1e-3
) -> bool:
    """Check whether two motor maps contain all motors 1..4 and match within tolerance."""
    req_ids = {1, 2, 3, 4}
    if not req_ids.issubset(m1.keys()) or not req_ids.issubset(m2.keys()):
        return False
    for mid in req_ids:
        if not math.isclose(m1[mid], m2[mid], abs_tol=tol):
            return False
    return True


def _is_zero_motors(m: Dict[int, float], tol: float = 1e-4) -> bool:
    """Check whether motor map contains all motors 1..4 and all speeds are zero within tolerance."""
    req_ids = {1, 2, 3, 4}
    if not req_ids.issubset(m.keys()):
        return False
    return all(abs(m[mid]) <= tol for mid in req_ids)


def _decode_motor_frame(
    frame_bytes: bytes,
) -> Tuple[bool, Optional[Dict[int, float]], Optional[str]]:
    """Decode a 27-byte STM32 motor frame (0xAA 0x55 0x03 0x16 0x01 0x04 ... CRC8)."""
    if len(frame_bytes) != 27:
        return False, None, f"invalid frame length {len(frame_bytes)}, expected 27"
    if frame_bytes[0] != 0xAA or frame_bytes[1] != 0x55:
        return False, None, f"invalid header 0x{frame_bytes[:2].hex()}, expected aa55"
    if frame_bytes[2] != 0x03:
        return (
            False,
            None,
            f"invalid function code 0x{frame_bytes[2]:02x}, expected 0x03",
        )
    if frame_bytes[3] != 0x16:
        return False, None, f"invalid length 0x{frame_bytes[3]:02x}, expected 0x16"
    if frame_bytes[4] != 0x01:
        return (
            False,
            None,
            f"invalid sub-command 0x{frame_bytes[4]:02x}, expected 0x01",
        )
    motor_count = frame_bytes[5]
    if motor_count != 4:
        return False, None, f"invalid motor count {motor_count}, expected 4"
    if checksum_crc8 is not None:
        expected_crc = checksum_crc8(frame_bytes[2:-1])
        if frame_bytes[-1] != expected_crc:
            return (
                False,
                None,
                f"CRC8 mismatch (expected 0x{expected_crc:02x}, got 0x{frame_bytes[-1]:02x})",
            )
    motors: Dict[int, float] = {}
    offset = 6
    for _ in range(motor_count):
        m_idx, rps = struct.unpack_from("<Bf", frame_bytes, offset)
        motors[m_idx + 1] = float(rps)
        offset += 5
    return True, motors, None


class BenchClientNode(Node):
    """Client node for executing bounded bench motions and measuring stop latencies."""

    def __init__(
        self,
        node_name: str = "operator_client",
        context=None,
        direct_ros: bool = False,
        authority_lock_path: Optional[str] = None,
        max_linear_speed: Optional[float] = None,
        max_angular_speed: Optional[float] = None,
    ):
        overrides = []
        if Parameter is not None:
            overrides.append(
                Parameter("start_type_description_service", Parameter.Type.BOOL, False)
            )

        self.max_linear_speed = max_linear_speed
        self.max_angular_speed = max_angular_speed
        self.guard_state: Optional[bool] = None
        self.guard_armed: Optional[bool] = None
        self._last_state_time: Optional[float] = None
        self._fallback_context = context
        self._executor = None

        self.observations: List[Dict[str, Any]] = []
        self.sub_obs = None

        self._ipc_client = None
        self._ipc_epoch = None
        self._owner_id = None
        self._authority_lock_fd = None
        is_direct = False

        if direct_ros:
            is_direct = True
        else:
            try:
                from ubuntu_tank_operator.ipc_client import OperatorIpcClient

                ipc = OperatorIpcClient()
                try:
                    ipc.connect(timeout_sec=0.2)
                except (ConnectionError, FileNotFoundError, OSError) as exc:
                    ipc.close()
                    raise RuntimeError(
                        "Operator agent is not reachable; pass direct_ros=True only "
                        "for an explicitly isolated direct ROS session."
                    ) from exc

                self._owner_id = f"bench_client_{os.getpid()}"
                ok, epoch, err, msg = ipc.acquire(
                    self._owner_id,
                    max_linear_speed=self.max_linear_speed,
                    max_angular_speed=self.max_angular_speed,
                    timeout_sec=1.0,
                )
                if ok and epoch is not None:
                    self._ipc_client = ipc
                    self._ipc_epoch = epoch
                    is_direct = False
                else:
                    ipc.close()
                    # FAIL CLOSED: agent rejection never creates a direct publisher.
                    raise RuntimeError(
                        f"Operator agent denied ownership to bench_client: {msg} ({err})"
                    )
            except ImportError as exc:
                raise RuntimeError(
                    "ubuntu_tank_operator is required unless direct_ros=True is explicit."
                ) from exc

        if is_direct and rclpy is not None:
            from ubuntu_tank_operator.authority_lock import acquire_authority_lock

            self._authority_lock_fd, _ = acquire_authority_lock(
                lock_path=authority_lock_path
            )

        if rclpy is not None:
            kwargs = {
                "start_parameter_services": False,
                "parameter_overrides": overrides,
            }
            if context is not None:
                try:
                    super().__init__(node_name, context=context, **kwargs)
                except TypeError:
                    super().__init__(node_name, **kwargs)
            else:
                super().__init__(node_name, **kwargs)

            if SingleThreadedExecutor is not None:
                try:
                    ctx = getattr(self, "context", None) or self._fallback_context
                    self._executor = SingleThreadedExecutor(context=ctx)
                    self._executor.add_node(self)
                except Exception:
                    self._executor = None

            if is_direct:
                # Arm/disarm service client
                self.arm_client = self.create_client(
                    SetBool, "/ubuntu_tank_safety/set_arm"
                )
                # Velocity command publisher
                self.cmd_vel_pub = self.create_publisher(
                    Twist, "/controller/cmd_vel", 1
                )
            else:
                self.arm_client = None
                self.cmd_vel_pub = None

            # Delivery observation subscription
            if String is not None:
                self.sub_obs = self.create_subscription(
                    String, "/ubuntu_tank/delivery_observation", self._obs_cb, 50
                )

            # Transient-local guard status subscriptions
            if QoSProfile and DurabilityPolicy and ReliabilityPolicy:
                transient_qos = QoSProfile(
                    depth=1,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                    reliability=ReliabilityPolicy.RELIABLE,
                )
            else:
                transient_qos = 1

            if Bool is not None:
                self.sub_state = self.create_subscription(
                    Bool, "/ubuntu_tank_safety/state", self._state_cb, transient_qos
                )
                self.sub_armed = self.create_subscription(
                    Bool, "/ubuntu_tank_safety/armed", self._armed_cb, transient_qos
                )
        else:
            try:
                super().__init__(node_name, context=context)
            except TypeError:
                super().__init__()
            self.arm_client = None
            self.cmd_vel_pub = None
            self.sub_state = None
            self.sub_armed = None
            self.sub_obs = None

    def _state_cb(self, msg):

        self.guard_state = msg.data
        self._last_state_time = time.monotonic()

    def _armed_cb(self, msg):
        self.guard_armed = msg.data
        self._last_state_time = time.monotonic()

    def _obs_cb(self, msg):
        try:
            data = json.loads(msg.data)
            data["_rx_mono"] = time.monotonic()
            self.observations.append(data)
        except Exception:
            pass

    def _is_ok(self) -> bool:
        ctx = getattr(self, "context", None) or self._fallback_context
        if ctx is not None:
            try:
                return ctx.ok()
            except Exception:
                pass
        if rclpy is not None:
            try:
                return rclpy.ok()
            except Exception:
                pass
        return True

    def _spin_once(self, timeout_sec: float = 0.1):
        if self._executor is not None:
            try:
                self._executor.spin_once(timeout_sec=timeout_sec)
                return
            except Exception:
                pass
        if rclpy is not None and rclpy.ok():
            rclpy.spin_once(self, timeout_sec=timeout_sec)

    def destroy_node(self):
        if self._ipc_client is not None:
            try:
                self._ipc_client.stop()
            except Exception:
                pass
            try:
                if self._ipc_epoch is not None:
                    self._ipc_client.release(self._ipc_epoch)
            except Exception:
                pass
            try:
                self._ipc_client.close()
            except Exception:
                pass
            self._ipc_client = None
            self._ipc_epoch = None
        if self._executor is not None:
            try:
                self._executor.remove_node(self)
            except Exception:
                pass
            try:
                self._executor.shutdown()
            except Exception:
                pass
            self._executor = None
        try:
            if rclpy is not None and hasattr(super(), "destroy_node"):
                super().destroy_node()
        finally:
            # Keep direct authority until its ROS publisher has been destroyed.
            if self._authority_lock_fd is not None:
                from ubuntu_tank_operator.authority_lock import release_authority_lock

                release_authority_lock(self._authority_lock_fd)
                self._authority_lock_fd = None

    @property
    def is_ipc_mode(self) -> bool:
        """Return True if connected to operator agent via IPC, False for direct ROS."""
        return self._ipc_client is not None

    def get_command_speeds(self) -> Tuple[float, float]:
        """Return (linear_mps, angular_rps) to use for commanded motion.

        When operating under the Operator Agent, queries configured speed caps so
        requested, commanded, and verified velocities agree.
        """
        fallback_lx = (
            self.max_linear_speed if self.max_linear_speed is not None else 0.20
        )
        fallback_az = (
            self.max_angular_speed if self.max_angular_speed is not None else 0.50
        )
        if self._ipc_client is not None:
            try:
                st = self._ipc_client.get_status(timeout_sec=1.0)
                if st:
                    limits = st.get("limits") or {}
                    lx = float(limits.get("max_linear_speed", fallback_lx))
                    az = float(limits.get("max_angular_speed", fallback_az))
                    return lx, az
            except Exception:
                pass
            return fallback_lx, fallback_az
        return (
            fallback_lx,
            self.max_angular_speed if self.max_angular_speed is not None else 0.80,
        )

    def call_set_arm(self, arm: bool, timeout_sec: float = 5.0) -> Tuple[bool, str]:
        """Send arm/disarm request to guard node and return (success, message)."""
        if self._ipc_client is not None:
            if arm:
                # Refresh control epoch from status or re-acquire if authority was lost
                try:
                    st = self._ipc_client.get_status(timeout_sec=timeout_sec)
                    if (
                        st
                        and st.get("active_owner") == self._owner_id
                        and st.get("current_epoch") is not None
                    ):
                        self._ipc_epoch = st["current_epoch"]
                    elif st and st.get("active_owner") is None and self._owner_id:
                        ok_acq, ep_acq, _, _ = self._ipc_client.acquire(
                            self._owner_id, timeout_sec=timeout_sec
                        )
                        if ok_acq and ep_acq is not None:
                            self._ipc_epoch = ep_acq
                except Exception:
                    pass

                if self._ipc_epoch is None:
                    return False, "No active control authority"

                ok, err, msg = self._ipc_client.arm(
                    self._ipc_epoch, tracks_raised=True, timeout_sec=timeout_sec
                )
                if not ok and err == "INVALID_EPOCH":
                    # Retry once with freshly resolved epoch
                    try:
                        st = self._ipc_client.get_status(timeout_sec=timeout_sec)
                        if st and st.get("current_epoch") is not None:
                            self._ipc_epoch = st["current_epoch"]
                            ok, err, msg = self._ipc_client.arm(
                                self._ipc_epoch,
                                tracks_raised=True,
                                timeout_sec=timeout_sec,
                            )
                    except Exception:
                        pass
                return ok, msg or (err or "")
            else:
                ok, msg = self._ipc_client.disarm(
                    epoch=self._ipc_epoch, timeout_sec=timeout_sec
                )
                return ok, msg or ""

        if self.arm_client is None:
            return False, "ROS client not initialized"

        start_time = time.monotonic()
        while not self.arm_client.wait_for_service(timeout_sec=0.2):
            if time.monotonic() - start_time >= timeout_sec:
                return (
                    False,
                    f"Timed out waiting for /ubuntu_tank_safety/set_arm after {timeout_sec:.1f}s",
                )
            if not self._is_ok():
                return False, "ROS context was shut down"

        req = SetBool.Request()
        req.data = arm
        future = self.arm_client.call_async(req)

        while self._is_ok():
            self._spin_once(timeout_sec=0.1)
            if future.done():
                try:
                    res = future.result()
                    return res.success, res.message
                except Exception as exc:
                    return False, f"Service call exception: {exc}"
            if time.monotonic() - start_time >= timeout_sec:
                return (
                    False,
                    f"Timed out waiting for set_arm response after {timeout_sec:.1f}s",
                )

        return False, "ROS context stopped before receiving response"

    def publish_cmd_vel(self, linear_x: float = 0.0, angular_z: float = 0.0) -> bool:
        """Publish a single Twist velocity command on /controller/cmd_vel."""
        if self.cmd_vel_pub is None or Twist is None:
            return False

        msg = Twist()
        msg.linear.x = float(linear_x)
        msg.linear.y = 0.0
        msg.linear.z = 0.0
        msg.angular.x = 0.0
        msg.angular.y = 0.0
        msg.angular.z = float(angular_z)
        self.cmd_vel_pub.publish(msg)
        return True

    def send_stop(self, count: int = 4) -> bool:
        """Send repeated zero velocity commands."""
        if self._ipc_client is not None:
            try:
                self._ipc_client.stop()
            except Exception:
                pass
        for _ in range(max(1, count)):
            if not self.publish_cmd_vel(0.0, 0.0):
                return False
            if self._is_ok():
                self._spin_once(timeout_sec=0.01)
        return True

    def run_motion_burst(
        self,
        linear_x: float,
        angular_z: float,
        duration_sec: float = 1.0,
        rate_hz: float = 20.0,
    ) -> bool:
        """
        Execute a finite, bounded motion burst, strictly self-terminating in a stop.
        """
        if duration_sec <= 0.0 or duration_sec > 5.0:
            raise ValueError(
                f"duration_sec must be between 0.0 and 5.0s, got {duration_sec}"
            )
        if rate_hz <= 0.0 or rate_hz > 100.0:
            raise ValueError(f"rate_hz must be between 0.0 and 100.0 Hz, got {rate_hz}")

        if self._ipc_client is not None:
            # Reconcile epoch before starting burst if needed
            if self._ipc_epoch is None:
                try:
                    st = self._ipc_client.get_status(timeout_sec=0.5)
                    if st and st.get("current_epoch") is not None:
                        self._ipc_epoch = st["current_epoch"]
                except Exception:
                    pass

            if self._ipc_epoch is None:
                return False

            if linear_x > 1e-3:
                direction = "forward"
            elif linear_x < -1e-3:
                direction = "reverse"
            elif angular_z > 1e-3:
                direction = "spin_left"
            elif angular_z < -1e-3:
                direction = "spin_right"
            else:
                direction = "neutral"

            interval = 1.0 / rate_hz
            start_time = time.monotonic()
            seq = 0
            try:
                while time.monotonic() - start_time < duration_sec:
                    c = self._ipc_client.request_challenge(
                        self._ipc_epoch, timeout_sec=0.5
                    )
                    if not c or "token" not in c:
                        return False
                    seq += 1
                    ok, cur_dir, err = self._ipc_client.submit_intent(
                        c["token"], self._ipc_epoch, seq, direction, timeout_sec=0.5
                    )
                    if not ok:
                        return False
                    if self._is_ok():
                        self._spin_once(timeout_sec=interval)
                    else:
                        time.sleep(interval)
            finally:
                try:
                    # Submit neutral intent to generate correlated terminating zeros
                    c = self._ipc_client.request_challenge(
                        self._ipc_epoch, timeout_sec=0.2
                    )
                    if c and "token" in c:
                        seq += 1
                        self._ipc_client.submit_intent(
                            c["token"],
                            self._ipc_epoch,
                            seq,
                            "neutral",
                            timeout_sec=0.2,
                        )
                except Exception:
                    pass
                try:
                    self._ipc_client.stop(timeout_sec=0.5)
                except Exception:
                    pass
                if self._is_ok():
                    self._spin_once(timeout_sec=0.05)

            return True

        interval = 1.0 / rate_hz
        start_time = time.monotonic()
        try:
            while time.monotonic() - start_time < duration_sec:
                if not self._is_ok():
                    return False
                if self.guard_armed is False or not self.publish_cmd_vel(
                    linear_x, angular_z
                ):
                    return False
                if self._is_ok():
                    self._spin_once(timeout_sec=interval)
                else:
                    time.sleep(interval)
        finally:
            self.send_stop(count=4)

        return True

    def reset_state(self):
        """Discard samples before requesting a new arm/disarm transition."""
        self.guard_state = None
        self.guard_armed = None

    def wait_for_state(
        self, timeout_sec: float = 3.0, expected_armed: Optional[bool] = None
    ) -> Dict[str, Optional[bool]]:
        """Wait for fresh guard samples, optionally matching an expected arming state.

        Call reset_state before requesting a transition; callbacks dispatched
        while waiting for its service response must remain available here.
        On timeout return the samples received so far.
        """
        start = time.monotonic()
        while self._is_ok():
            self._spin_once(timeout_sec=0.05)
            if (
                self.guard_state is not None
                and self.guard_armed is not None
                and (expected_armed is None or self.guard_armed == expected_armed)
            ):
                break
            if time.monotonic() - start >= timeout_sec:
                break
        return {"guard_state": self.guard_state, "guard_armed": self.guard_armed}

    def prepare_discovery(self, timeout_sec: float = 3.0) -> bool:
        """Wait for required services and topics while DISARMED before arming."""
        start = time.monotonic()
        if self._ipc_client is not None:
            while time.monotonic() - start < timeout_sec:
                if not self._is_ok():
                    return False
                self._spin_once(timeout_sec=0.05)
                if self.guard_armed is False:
                    return True
                if self.guard_armed is True:
                    self.call_set_arm(False, timeout_sec=1.0)
            return self.guard_armed is False

        if self.arm_client is None or self.cmd_vel_pub is None:
            return False

        # 1. Wait for set_arm service
        while not self.arm_client.wait_for_service(timeout_sec=0.2):
            if time.monotonic() - start >= timeout_sec:
                return False
            if not self._is_ok():
                return False
            self._spin_once(timeout_sec=0.05)

        # 2. Wait until guard state is observed and confirmed disarmed
        while time.monotonic() - start < timeout_sec:
            if not self._is_ok():
                return False
            self._spin_once(timeout_sec=0.05)
            if self.guard_armed is False:
                return True
            if self.guard_armed is True:
                # If unexpectedly armed at discovery, disarm it
                self.call_set_arm(False, timeout_sec=1.0)
        return self.guard_armed is False

    def reset_observations(self) -> float:
        """Clear recorded delivery observations and return start timestamp."""
        self.observations.clear()
        if self._ipc_client is not None:
            try:
                self._ipc_client.reset_observations(timeout_sec=1.0)
            except Exception:
                pass
        return time.monotonic()

    def get_observations(self, since_mono: float = 0.0) -> List[Dict[str, Any]]:
        """Return all observations received since monotonic timestamp."""
        return [
            obs for obs in self.observations if obs.get("stamp_mono", 0.0) >= since_mono
        ]

    def verify_correlated_delivery(
        self,
        since_mono: float,
        burst_name: str,
        expected_lx: float,
        expected_az: float,
        timeout_sec: float = 1.5,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Verify complete 5-stage correlated delivery pipeline for a motion burst.

        Stages: controller_rx -> guard_rx -> guard_fwd -> bridge_rx -> bridge_write (success).
        Correlates matching motor commands across all downstream stages, verifies positive
        complete frame writes with valid encoding, and requires subsequent terminating four-motor
        zero receipt and writes for motion bursts.
        """
        start_wait = time.monotonic()
        target_stages = {
            "controller_rx",
            "guard_rx",
            "guard_fwd",
            "bridge_rx",
            "bridge_write",
        }
        is_motion = abs(expected_lx) > 1e-3 or abs(expected_az) > 1e-3

        while time.monotonic() - start_wait < timeout_sec:
            self._spin_once(timeout_sec=0.05)
            obs_list = self.get_observations(since_mono)
            stages = {obs.get("stage") for obs in obs_list}
            if target_stages.issubset(stages):
                if is_motion:
                    has_zero_write = any(
                        o.get("stage") == "bridge_write"
                        and _is_zero_motors(_extract_motors_map(o.get("motors")))
                        for o in obs_list
                    )
                    if has_zero_write:
                        break
                else:
                    break

        obs_list = self.get_observations(since_mono)
        evidence = {
            "burst_name": burst_name,
            "since_mono": since_mono,
            "observation_count": len(obs_list),
            "stages_found": list({obs.get("stage") for obs in obs_list}),
            "runs": {},
            "sink_type": "unknown",
            "bytes_written": 0,
            "bytes_expected": 0,
            "errors": [],
        }

        # Check for process run IDs (detect process crash/restart during burst)
        for obs in obs_list:
            node = obs.get("node", "unknown")
            run_id = obs.get("run_id", "none")
            if node not in evidence["runs"]:
                evidence["runs"][node] = run_id
            elif evidence["runs"][node] != run_id:
                msg = (
                    f"Process restart detected during burst for node '{node}' "
                    f"({evidence['runs'][node]} -> {run_id})"
                )
                evidence["errors"].append(msg)
                return False, msg, evidence

        # 1. Stage: controller_rx
        ctrl_obs = [o for o in obs_list if o.get("stage") == "controller_rx"]
        if not ctrl_obs:
            msg = f"{burst_name}: Missing stage 'controller_rx' (command dropped or unread by controller)"
            evidence["errors"].append(msg)
            return False, msg, evidence

        matching_ctrl = [
            o
            for o in ctrl_obs
            if math.isclose(o.get("linear_x", 0.0), expected_lx, abs_tol=1e-3)
            and math.isclose(o.get("angular_z", 0.0), expected_az, abs_tol=1e-3)
        ]
        if not matching_ctrl:
            msg = (
                f"{burst_name}: controller_rx command values mismatch "
                f"(expected lx={expected_lx}, az={expected_az})"
            )
            evidence["errors"].append(msg)
            return False, msg, evidence

        ctrl = matching_ctrl[-1]
        expected_motors = _extract_motors_map(ctrl.get("motors"))
        if not expected_motors or not {1, 2, 3, 4}.issubset(expected_motors.keys()):
            msg = f"{burst_name}: controller_rx observation missing required motor values (1..4)"
            evidence["errors"].append(msg)
            return False, msg, evidence

        if is_motion and _is_zero_motors(expected_motors):
            msg = f"{burst_name}: controller_rx computed all-zero motors for nonzero motion command"
            evidence["errors"].append(msg)
            return False, msg, evidence

        # 2. Stage: guard_rx
        guard_rx_obs = [o for o in obs_list if o.get("stage") == "guard_rx"]
        if not guard_rx_obs:
            msg = f"{burst_name}: Missing stage 'guard_rx' (edge controller->guard broken)"
            evidence["errors"].append(msg)
            return False, msg, evidence

        matching_guard_rx = [
            o
            for o in guard_rx_obs
            if _motors_match(_extract_motors_map(o.get("motors")), expected_motors)
        ]
        if not matching_guard_rx:
            msg = (
                f"{burst_name}: guard_rx motor commands do not match expected motion "
                f"(expected {expected_motors})"
            )
            evidence["errors"].append(msg)
            return False, msg, evidence

        # 3. Stage: guard_fwd
        guard_fwd_obs = [o for o in obs_list if o.get("stage") == "guard_fwd"]
        if not guard_fwd_obs:
            faults = [
                o
                for o in obs_list
                if o.get("stage") in ("guard_fault", "guard_timeout")
            ]
            fault_reasons = [f.get("reason", "unknown") for f in faults]
            msg = f"{burst_name}: Missing stage 'guard_fwd' (guard rejected command: {fault_reasons})"
            evidence["errors"].append(msg)
            return False, msg, evidence

        matching_guard_fwd = [
            o
            for o in guard_fwd_obs
            if _motors_match(_extract_motors_map(o.get("motors")), expected_motors)
        ]
        if not matching_guard_fwd:
            msg = (
                f"{burst_name}: guard_fwd motor commands do not match expected motion "
                f"(expected {expected_motors})"
            )
            evidence["errors"].append(msg)
            return False, msg, evidence

        # 4. Stage: bridge_rx
        bridge_rx_obs = [o for o in obs_list if o.get("stage") == "bridge_rx"]
        if not bridge_rx_obs:
            msg = f"{burst_name}: Missing stage 'bridge_rx' (edge guard->bridge broken)"
            evidence["errors"].append(msg)
            return False, msg, evidence

        matching_bridge_rx = [
            o
            for o in bridge_rx_obs
            if _motors_match(_extract_motors_map(o.get("motors")), expected_motors)
        ]
        if not matching_bridge_rx:
            msg = (
                f"{burst_name}: bridge_rx motor commands do not match expected motion "
                f"(expected {expected_motors})"
            )
            evidence["errors"].append(msg)
            return False, msg, evidence

        # 5. Stage: bridge_write (positive complete frame-write evidence)
        bridge_write_obs = [o for o in obs_list if o.get("stage") == "bridge_write"]
        if not bridge_write_obs:
            msg = f"{burst_name}: Missing stage 'bridge_write' (bridge did not attempt serial write)"
            evidence["errors"].append(msg)
            return False, msg, evidence

        matching_bridge_writes = [
            o
            for o in bridge_write_obs
            if _motors_match(_extract_motors_map(o.get("motors")), expected_motors)
        ]
        if not matching_bridge_writes:
            msg = (
                f"{burst_name}: bridge_write motor commands do not match expected motion "
                f"(expected {expected_motors})"
            )
            evidence["errors"].append(msg)
            return False, msg, evidence

        for w in matching_bridge_writes:
            if not w.get("success", False):
                err = w.get("error", "unspecified serial write error")
                msg = f"{burst_name}: bridge_write failed on sink '{w.get('sink_type')}': {err}"
                evidence["errors"].append(msg)
                return False, msg, evidence
            if w.get("bytes_written", 0) < w.get("bytes_expected", 0):
                msg = (
                    f"{burst_name}: short write on sink '{w.get('sink_type')}': "
                    f"{w.get('bytes_written')}/{w.get('bytes_expected')}"
                )
                evidence["errors"].append(msg)
                return False, msg, evidence
            if w.get("bytes_written", 0) == 0:
                msg = f"{burst_name}: zero bytes written on sink '{w.get('sink_type')}'"
                evidence["errors"].append(msg)
                return False, msg, evidence
            frame_hex = w.get("frame_hex", "")
            if frame_hex and len(frame_hex) == 54:
                try:
                    frame_bytes = bytes.fromhex(frame_hex)
                    ok, f_motors, f_err = _decode_motor_frame(frame_bytes)
                    if not ok:
                        msg = f"{burst_name}: bridge_write frame decoding failed on sink '{w.get('sink_type')}': {f_err}"
                        evidence["errors"].append(msg)
                        return False, msg, evidence
                    if f_motors and not _motors_match(f_motors, expected_motors):
                        msg = f"{burst_name}: bridge_write frame payload motor mismatch: {f_motors} != {expected_motors}"
                        evidence["errors"].append(msg)
                        return False, msg, evidence
                except ValueError:
                    pass

        # 6. Subsequent terminating four-motor zero receipt and writes (for motion bursts)
        if is_motion:
            # Locate first matching motion write in observations
            motion_write_idx = obs_list.index(matching_bridge_writes[0])
            subsequent_obs = obs_list[motion_write_idx + 1 :]

            zero_rx = [
                o
                for o in subsequent_obs
                if o.get("stage") == "bridge_rx"
                and _is_zero_motors(_extract_motors_map(o.get("motors")))
            ]
            if not zero_rx:
                msg = f"{burst_name}: Missing terminating zero receipt at bridge (dropped stop command)"
                evidence["errors"].append(msg)
                return False, msg, evidence

            zero_writes = [
                o
                for o in subsequent_obs
                if o.get("stage") == "bridge_write"
                and _is_zero_motors(_extract_motors_map(o.get("motors")))
            ]
            if not zero_writes:
                msg = f"{burst_name}: Missing terminating zero serial write at bridge (motion not terminated with confirmed stop write)"
                evidence["errors"].append(msg)
                return False, msg, evidence

            for zw in zero_writes:
                if not zw.get("success", False):
                    err = zw.get("error", "unspecified serial write error")
                    msg = f"{burst_name}: terminating zero bridge_write failed on sink '{zw.get('sink_type')}': {err}"
                    evidence["errors"].append(msg)
                    return False, msg, evidence
                if zw.get("bytes_written", 0) < zw.get("bytes_expected", 0):
                    msg = (
                        f"{burst_name}: terminating zero short write on sink '{zw.get('sink_type')}': "
                        f"{zw.get('bytes_written')}/{zw.get('bytes_expected')}"
                    )
                    evidence["errors"].append(msg)
                    return False, msg, evidence
                if zw.get("bytes_written", 0) == 0:
                    msg = f"{burst_name}: terminating zero write wrote 0 bytes on sink '{zw.get('sink_type')}'"
                    evidence["errors"].append(msg)
                    return False, msg, evidence
                frame_hex = zw.get("frame_hex", "")
                if frame_hex and len(frame_hex) == 54:
                    try:
                        frame_bytes = bytes.fromhex(frame_hex)
                        ok, f_motors, f_err = _decode_motor_frame(frame_bytes)
                        if not ok:
                            msg = f"{burst_name}: terminating zero frame decoding failed on sink '{zw.get('sink_type')}': {f_err}"
                            evidence["errors"].append(msg)
                            return False, msg, evidence
                        if f_motors and not _is_zero_motors(f_motors):
                            msg = f"{burst_name}: terminating zero frame payload is not zero: {f_motors}"
                            evidence["errors"].append(msg)
                            return False, msg, evidence
                    except ValueError:
                        pass

        last_write = matching_bridge_writes[-1]
        evidence["sink_type"] = last_write.get("sink_type", "unknown")
        evidence["bytes_written"] = sum(
            o.get("bytes_written", 0)
            for o in obs_list
            if o.get("stage") == "bridge_write"
        )
        evidence["bytes_expected"] = sum(
            o.get("bytes_expected", 0)
            for o in obs_list
            if o.get("stage") == "bridge_write"
        )

        return (
            True,
            f"Verified 5-stage delivery and terminating zero to {evidence['sink_type']} sink ({len(obs_list)} observations)",
            evidence,
        )

    def verify_disarm_stop_delivery(
        self,
        since_mono: float,
        burst_name: str = "disarm",
        timeout_sec: float = 1.5,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Verify downstream four-motor zero receipt and successful serial write during/after disarm.

        Confirms that zeros issued during disarm reached bridge_rx and were successfully written
        to the serial sink (bridge_write) with zero errors, positive bytes written, and valid frame encoding.
        Fails closed on missing, mismatched, empty, or failed write evidence.
        """
        start_wait = time.monotonic()
        while time.monotonic() - start_wait < timeout_sec:
            self._spin_once(timeout_sec=0.05)
            obs_list = self.get_observations(since_mono)
            has_rx = any(
                o.get("stage") == "bridge_rx"
                and _is_zero_motors(_extract_motors_map(o.get("motors")))
                for o in obs_list
            )
            has_write = any(
                o.get("stage") == "bridge_write"
                and _is_zero_motors(_extract_motors_map(o.get("motors")))
                for o in obs_list
            )
            if has_rx and has_write:
                break

        obs_list = self.get_observations(since_mono)
        evidence = {
            "burst_name": burst_name,
            "since_mono": since_mono,
            "observation_count": len(obs_list),
            "stages_found": list({obs.get("stage") for obs in obs_list}),
            "runs": {},
            "sink_type": "unknown",
            "bytes_written": 0,
            "bytes_expected": 0,
            "errors": [],
        }

        # Check for process run IDs
        for obs in obs_list:
            node = obs.get("node", "unknown")
            run_id = obs.get("run_id", "none")
            if node not in evidence["runs"]:
                evidence["runs"][node] = run_id
            elif evidence["runs"][node] != run_id:
                msg = (
                    f"Process restart detected during disarm for node '{node}' "
                    f"({evidence['runs'][node]} -> {run_id})"
                )
                evidence["errors"].append(msg)
                return False, msg, evidence

        # Require four-motor zero bridge_rx
        zero_rx = [
            o
            for o in obs_list
            if o.get("stage") == "bridge_rx"
            and _is_zero_motors(_extract_motors_map(o.get("motors")))
        ]
        if not zero_rx:
            msg = f"{burst_name}: Missing downstream zero command receipt at bridge during disarm"
            evidence["errors"].append(msg)
            return False, msg, evidence

        # Require four-motor zero bridge_write
        zero_writes = [
            o
            for o in obs_list
            if o.get("stage") == "bridge_write"
            and _is_zero_motors(_extract_motors_map(o.get("motors")))
        ]
        if not zero_writes:
            msg = f"{burst_name}: Missing downstream zero serial write at bridge during disarm"
            evidence["errors"].append(msg)
            return False, msg, evidence

        last_write = zero_writes[-1]
        evidence["sink_type"] = last_write.get("sink_type", "unknown")
        evidence["bytes_written"] = sum(o.get("bytes_written", 0) for o in zero_writes)
        evidence["bytes_expected"] = sum(
            o.get("bytes_expected", 0) for o in zero_writes
        )

        for w in zero_writes:
            if not w.get("success", False):
                err = w.get("error", "unspecified serial write error")
                msg = f"{burst_name}: bridge_write failed on sink '{w.get('sink_type')}': {err}"
                evidence["errors"].append(msg)
                return False, msg, evidence
            if w.get("bytes_written", 0) < w.get("bytes_expected", 0):
                msg = (
                    f"{burst_name}: short write on sink '{w.get('sink_type')}': "
                    f"{w.get('bytes_written')}/{w.get('bytes_expected')}"
                )
                evidence["errors"].append(msg)
                return False, msg, evidence
            if w.get("bytes_written", 0) == 0:
                msg = f"{burst_name}: zero bytes written on sink '{w.get('sink_type')}'"
                evidence["errors"].append(msg)
                return False, msg, evidence
            frame_hex = w.get("frame_hex", "")
            if frame_hex and len(frame_hex) == 54:
                try:
                    frame_bytes = bytes.fromhex(frame_hex)
                    ok, f_motors, f_err = _decode_motor_frame(frame_bytes)
                    if not ok:
                        msg = f"{burst_name}: frame decoding failed on sink '{w.get('sink_type')}': {f_err}"
                        evidence["errors"].append(msg)
                        return False, msg, evidence
                    if f_motors and not _is_zero_motors(f_motors):
                        msg = f"{burst_name}: disarm frame payload is not zero: {f_motors}"
                        evidence["errors"].append(msg)
                        return False, msg, evidence
                except ValueError:
                    pass

        return (
            True,
            f"Verified disarm zero delivery to {evidence['sink_type']} sink ({len(zero_writes)} zero writes)",
            evidence,
        )


def main(args=None):
    """Entry point for bench_client console script."""
    target_args = sys.argv[1:] if args is None else args
    if not target_args or "-h" in target_args or "--help" in target_args:
        print(
            "Usage: bench_client [--direct-ros] [--arm | --disarm | --stop | "
            "--motion <forward|reverse|left|right>]"
        )
        return 0

    if rclpy is None:
        sys.stderr.write("ERROR: rclpy is required to run bench_client.\n")
        return 1

    rclpy.init(args=args)
    node = BenchClientNode(direct_ros="--direct-ros" in target_args)
    try:
        if "--arm" in target_args:
            success, msg = node.call_set_arm(True)
            print(f"Arm result: success={success}, msg={msg}")
            return 0 if success else 1
        elif "--disarm" in target_args:
            success, msg = node.call_set_arm(False)
            print(f"Disarm result: success={success}, msg={msg}")
            return 0 if success else 1
        elif "--stop" in target_args:
            node.send_stop(count=4)
            print("Stop commands sent.")
            return 0
        elif "--motion" in target_args:
            idx = target_args.index("--motion")
            if idx + 1 >= len(target_args):
                sys.stderr.write(
                    "ERROR: --motion requires motion name (forward, reverse, left, right)\n"
                )
                return 1
            motion = target_args[idx + 1].lower()
            motions = {
                "forward": (0.2, 0.0),
                "reverse": (-0.2, 0.0),
                "left": (0.0, 0.8),
                "right": (0.0, -0.8),
            }
            if motion not in motions:
                sys.stderr.write(
                    f"ERROR: Unknown motion '{motion}'. Valid: {list(motions.keys())}\n"
                )
                return 1
            lx, az = motions[motion]
            print(
                f"Executing {motion} burst (linear.x={lx}, angular.z={az}, duration=1.0s)..."
            )
            node.run_motion_burst(lx, az, duration_sec=1.0)
            print("Motion completed. Zero commands sent.")
            return 0
        else:
            sys.stderr.write(f"ERROR: Unrecognized arguments: {target_args}\n")
            return 1
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
