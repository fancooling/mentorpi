#!/usr/bin/env python3
# encoding: utf-8
"""ROS 2 hardware bridge with guarded motor input and fatal serial handling."""

import os
import sys
import math
import time
import socket
import rclpy
import signal
import threading
import yaml
from rclpy.node import Node
from std_srvs.srv import Trigger
from sensor_msgs.msg import Imu, Joy
from std_msgs.msg import UInt16, Bool
from ros_robot_controller.ros_robot_controller_sdk import Board, PacketReportKeyEvents
from ros_robot_controller_msgs.srv import GetBusServoState, GetPWMServoState
from ros_robot_controller_msgs.msg import (
    ButtonState, BuzzerState, MotorsState, BusServoState, LedState,
    SetBusServoState, ServosPosition, SetPWMServoState, Sbus, OLEDState,
    RGBStates, PWMServoState
)

GUARDED_MOTOR_TOPIC = '/ros_robot_controller/set_motor_guarded'


class RosRobotController(Node):
    """Bridge validated motor commands and controller telemetry over `/dev/rrc`.

    Controller-only mode exposes only the guarded motor subscription and battery
    telemetry. Serial read/write failures suppress process heartbeats and stop
    ROS so the systemd supervisor can fail the complete controller graph closed.
    """

    gravity = 9.80665

    def __init__(self, name='ros_robot_controller'):
        super().__init__(name)

        # Declare ROS parameters
        self.declare_parameter('machine_type', 'MentorPi_Tank')
        self.declare_parameter('serial_device', '/dev/rrc')
        self.declare_parameter('baud_rate', 1000000)
        self.declare_parameter('controller_only', True)
        self.declare_parameter('freshness_timeout_sec', 0.250)
        self.declare_parameter('heartbeat_interval_sec', 0.200)
        self.declare_parameter('serial_read_timeout_sec', 0.050)
        self.declare_parameter('write_timeout_sec', 0.100)
        self.declare_parameter('serial_silence_timeout_sec', 0.500)
        self.declare_parameter('servo_config_file', '/etc/opt/ubuntu_tank/servo_config.yaml')
        self.declare_parameter('load_servo_offsets', False)
        self.declare_parameter('imu_frame', 'imu_link')
        self.declare_parameter('init_finish', False)
        self.declare_parameter('motor_topic', GUARDED_MOTOR_TOPIC)

        # Retrieve parameter values
        self.machine_type = str(self.get_parameter('machine_type').value)
        self.serial_device = str(self.get_parameter('serial_device').value)
        self.baud_rate = int(self.get_parameter('baud_rate').value)
        self.controller_only = bool(self.get_parameter('controller_only').value)
        self.freshness_timeout_sec = float(self.get_parameter('freshness_timeout_sec').value)
        self.heartbeat_interval_sec = float(self.get_parameter('heartbeat_interval_sec').value)
        self.serial_read_timeout_sec = float(self.get_parameter('serial_read_timeout_sec').value)
        self.write_timeout_sec = float(self.get_parameter('write_timeout_sec').value)
        self.serial_silence_timeout_sec = float(self.get_parameter('serial_silence_timeout_sec').value)
        self.servo_config_file = str(self.get_parameter('servo_config_file').value)
        self.load_servo_offsets_flag = bool(self.get_parameter('load_servo_offsets').value)
        self.IMU_FRAME = str(self.get_parameter('imu_frame').value)
        self.motor_topic = str(self.get_parameter('motor_topic').value)

        if not math.isfinite(self.freshness_timeout_sec) or self.freshness_timeout_sec <= 0:
            raise ValueError('freshness_timeout_sec must be finite and greater than zero')
        if (
            not math.isfinite(self.write_timeout_sec)
            or self.write_timeout_sec <= 0
            or self.write_timeout_sec >= self.freshness_timeout_sec
        ):
            raise ValueError('write_timeout_sec must be finite, positive, and shorter than freshness_timeout_sec')
        if (
            not math.isfinite(self.serial_read_timeout_sec)
            or self.serial_read_timeout_sec <= 0
            or not math.isfinite(self.serial_silence_timeout_sec)
            or self.serial_silence_timeout_sec <= self.serial_read_timeout_sec
        ):
            raise ValueError('serial timeouts must be finite, positive, and silence must exceed read timeout')
        if self.controller_only and self.motor_topic != GUARDED_MOTOR_TOPIC:
            raise ValueError(f'controller_only requires motor_topic={GUARDED_MOTOR_TOPIC}')

        # Initialize hardware board interface
        self.board = Board(
            device=self.serial_device,
            baudrate=self.baud_rate,
            timeout=self.serial_read_timeout_sec,
            write_timeout=self.write_timeout_sec,
            silence_timeout=self.serial_silence_timeout_sec,
        )
        self.board.enable_reception()
        self.running = True
        self._shutting_down = False
        self._fatal_fault = False
        self._motor_lock = threading.RLock()
        self._last_motor_cmd_time = None

        # Heartbeat communication channels (inherited pipe FD or UNIX domain socket)
        bridge_fd_str = os.environ.get('UBUNTU_TANK_BRIDGE_HEARTBEAT_FD') or os.environ.get('UBUNTU_TANK_BRIDGE_PIPE_FD')
        self.bridge_pipe_fd = int(bridge_fd_str) if bridge_fd_str and bridge_fd_str.isdigit() else None
        self.bridge_sock_path = os.environ.get('UBUNTU_TANK_BRIDGE_SOCK', '/run/ubuntu_tank/bridge_heartbeat.sock')

        bridge_pid_file = os.environ.get('UBUNTU_TANK_BRIDGE_PID_FILE', '/run/ubuntu_tank/bridge.pid')
        if os.path.exists(os.path.dirname(bridge_pid_file)):
            try:
                with open(bridge_pid_file, 'w', encoding='utf-8') as pf:
                    pf.write(str(os.getpid()))
            except Exception:
                pass

        # Telemetry battery publisher (always available)
        self.battery_pub = self.create_publisher(UInt16, '~/battery', 1)

        # Motor command subscription (remapped to guarded output by default)
        self.create_subscription(MotorsState, self.motor_topic, self.set_motor_state, 10)

        # Read-only readiness service remains available in controller-only mode.
        self.create_service(Trigger, '~/init_finish', self.get_node_state)

        if self.controller_only:
            # Dedicated battery telemetry polling in controller-only mode (1 Hz)
            self.board.enable_reception(True)
            self.battery_timer = self.create_timer(1.0, self._battery_timer_callback)
        else:
            # Legacy peripherals publishers and subscriptions
            self.imu_pub = self.create_publisher(Imu, '~/imu_raw', 1)
            self.joy_pub = self.create_publisher(Joy, '~/joy', 1)
            self.sbus_pub = self.create_publisher(Sbus, '~/sbus', 1)
            self.button_pub = self.create_publisher(ButtonState, '~/button', 1)
            self.create_subscription(LedState, '~/set_led', self.set_led_state, 5)
            self.create_subscription(BuzzerState, '~/set_buzzer', self.set_buzzer_state, 5)
            self.create_subscription(OLEDState, '~/set_oled', self.set_oled_state, 5)
            self.create_subscription(Bool, '~/enable_reception', self.enable_reception, 1)
            self.create_subscription(SetBusServoState, '~/bus_servo/set_state', self.set_bus_servo_state, 10)
            self.create_subscription(ServosPosition, '~/bus_servo/set_position', self.set_bus_servo_position, 10)
            self.create_subscription(SetPWMServoState, '~/pwm_servo/set_state', self.set_pwm_servo_state, 10)
            self.create_service(GetBusServoState, '~/bus_servo/get_state', self.get_bus_servo_state)
            self.create_service(GetPWMServoState, '~/pwm_servo/get_state', self.get_pwm_servo_state)
            self.create_service(Trigger, '~/set_machine_type', self.set_machine_type)
            self.create_subscription(RGBStates, '~/set_rgb', self.set_rgb_states, 10)

            if self.load_servo_offsets_flag:
                self.load_servo_offsets()
            threading.Thread(target=self.pub_callback, daemon=True).start()

        # Send machine type and initialize motor speed to zero
        self.motor_type = None
        self.battery_level = None
        self.send_machine_type()
        self.board.zero_motors(count=2)

        # Monotonic freshness watchdog thread
        threading.Thread(target=self._motor_watchdog_loop, daemon=True).start()

        # Supervisor heartbeat emission timer
        self.clock = self.get_clock()
        self.heartbeat_timer = self.create_timer(self.heartbeat_interval_sec, self._emit_heartbeat)

        self.get_logger().info('ros_robot_controller initialized (controller_only=%s)' % self.controller_only)

    def _battery_timer_callback(self):
        """Poll and publish battery telemetry in controller-only mode."""
        if not self._fatal_fault:
            self.pub_battery_data(self.battery_pub)

    def _enter_fatal_fault(self, reason):
        """Stop supervision and the ROS graph after an unrecoverable bridge fault."""
        with self._motor_lock:
            if self._fatal_fault:
                return
            self._fatal_fault = True
            self._last_motor_cmd_time = None
            try:
                self.get_logger().fatal(f"FATAL: {reason}; stopping graph")
            except Exception:
                pass
            if hasattr(self, 'heartbeat_timer') and self.heartbeat_timer is not None:
                try:
                    self.heartbeat_timer.cancel()
                except Exception:
                    pass
            self.safe_shutdown()
            try:
                rclpy.shutdown()
            except Exception:
                pass

    def _motor_watchdog_step(self):
        """Check monotonic freshness and board errors; enter fatal shutdown on expiry."""
        with self._motor_lock:
            board_error = getattr(self.board, 'fatal_error', None)
            if board_error is not None:
                self._enter_fatal_fault(str(board_error))
                return
            if self._last_motor_cmd_time is not None:
                elapsed = time.monotonic() - self._last_motor_cmd_time
                if elapsed > self.freshness_timeout_sec:
                    self._enter_fatal_fault(
                        f"Bridge freshness timeout ({elapsed:.3f}s > {self.freshness_timeout_sec:.3f}s)"
                    )

    def _motor_watchdog_loop(self):
        """Independent monotonic watchdog thread enforcing 250 ms freshness deadline."""
        while self.running and not self._fatal_fault:
            time.sleep(0.02)
            self._motor_watchdog_step()

    def _emit_heartbeat(self):
        """Emit monotonic timestamp heartbeat to supervisor."""
        with self._motor_lock:
            board_error = getattr(self.board, 'fatal_error', None)
            if board_error is not None:
                self._enter_fatal_fault(str(board_error))
            if self._fatal_fault or self._shutting_down:
                return
        payload = f"{time.monotonic()}\n".encode('ascii')
        if self.bridge_pipe_fd is not None:
            try:
                os.write(self.bridge_pipe_fd, payload)
                return
            except Exception as e:
                self.get_logger().debug(f"Bridge pipe heartbeat failed: {e}")
        if self.bridge_sock_path and os.path.exists(self.bridge_sock_path):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                    s.sendto(payload.strip(), self.bridge_sock_path)
            except Exception as e:
                self.get_logger().debug(f"Bridge socket heartbeat failed: {e}")

    def safe_shutdown(self):
        """Orderly signal-safe shutdown: send repeated zeros and close port."""
        with self._motor_lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            self.running = False
            try:
                self.get_logger().info("Initiating signal-safe bridge shutdown and motor zeroing...")
            except Exception:
                pass
            try:
                self.board.zero_motors(count=4)
            except Exception as e:
                try:
                    self.get_logger().error(f"Error zeroing motors during bridge shutdown: {e}")
                except Exception:
                    pass
            finally:
                try:
                    self.board.close()
                except Exception:
                    pass

    def load_servo_offsets(self):
        """
        Read servo offset settings from YAML file.
        """
        config_path = self.servo_config_file
        if not os.path.exists(config_path):
            self.get_logger().warn(f"Servo config file not found: {config_path}")
            return
        try:
            with open(config_path, 'r', encoding='utf-8') as file:
                config = yaml.safe_load(file)

            if not isinstance(config, dict):
                self.get_logger().error(f"YAML config format error: {config_path}, expected dict.")
                return

            for servo_id in range(1, 5):
                offset = config.get(servo_id, 0)
                try:
                    self.board.pwm_servo_set_offset(servo_id, offset)
                except Exception as e:
                    self.get_logger().error(f"Error setting servo {servo_id} offset: {e}")

        except Exception as e:
            self.get_logger().error(f"Error loading servo config {config_path}: {e}")

    def get_node_state(self, request, response):
        response.success = True
        return response

    def set_machine_type(self, request, response):
        self.send_machine_type()
        response.success = True
        response.message = 'FINISH!!!'
        return response
        
    def pub_callback(self):
        while self.running:
            if getattr(self, 'enable_reception', False):
                self.pub_button_data(self.button_pub)
                self.pub_joy_data(self.joy_pub)
                self.pub_imu_data(self.imu_pub)
                self.pub_sbus_data(self.sbus_pub)
                self.pub_battery_data(self.battery_pub)
                time.sleep(0.02)
            else:
                time.sleep(0.02)
        rclpy.shutdown()

    def enable_reception(self, msg):
        self.get_logger().info('\033[1;32m%s\033[0m' % ('enable_reception ' + str(msg.data)))
        self.enable_reception = msg.data
        self.board.enable_reception(msg.data)

    def set_led_state(self, msg):
        self.board.set_led(msg.on_time, msg.off_time, msg.repeat, msg.id)

    def set_buzzer_state(self, msg):
        self.board.set_buzzer(msg.freq, msg.on_time, msg.off_time, msg.repeat)
    
    def set_rgb_states(self, msg):
        pixels = []
        for state in msg.states:
            pixels.append((state.index, state.red, state.green, state.blue))
        self.board.set_rgb(pixels)

    def set_motor_state(self, msg):
        with self._motor_lock:
            if self._fatal_fault or self._shutting_down:
                self.get_logger().error("Bridge in fatal fault state; dropping motor command")
                return
            data = []
            for i in msg.data:
                data.extend([[i.id, i.rps]])
            try:
                self.board.set_motor_speed(data)
                # Advance freshness timestamp ONLY after successful serial write and when not in fatal/shutdown state
                if not self._fatal_fault and not self._shutting_down:
                    self._last_motor_cmd_time = time.monotonic()
            except Exception as e:
                self._enter_fatal_fault(f"Serial write error in bridge: {e}")

    def set_oled_state(self, msg):
        self.board.set_oled_text(int(msg.index), msg.text)

    def set_pwm_servo_state(self, msg):
        data = []
        for i in msg.state:
            if i.id and i.position:
                data.extend([[i.id[0], i.position[0]]])
            if i.id and i.offset:
                self.board.pwm_servo_set_offset(i.id[0], i.offset[0])

        if data != []:
            self.board.pwm_servo_set_position(msg.duration, data)

    def send_machine_type(self):
        if 'Tank' in self.machine_type:
            self.motor_type = 0x01
            self.battery_level = 0x1af4
        else:
            self.motor_type = 0x01
            self.battery_level = 0x1af4
        if self.motor_type is not None:
            for _ in range(2):
                try:
                    self.board.set_motor_type(self.motor_type)
                    self.board.set_battery_level(self.battery_level)
                except Exception as e:
                    self.get_logger().warn(f"Failed to send machine type to board: {e}")
        else:
            self.get_logger().info('Please Set the machine_type')


    def get_pwm_servo_state(self, msg):
        states = []
        for i in msg.cmd:
            data = PWMServoState()
            if i.get_position:
                state = self.board.pwm_servo_read_position(i.id)
                if state is not None:
                    data.position = state
            if i.get_offset:
                state = self.board.pwm_servo_read_offset(i.id)
                if state is not None:
                    data.offset = state
            states.append(data)
        return [True, states]

    def set_bus_servo_position(self, msg):
        data = []
        for i in msg.position:
            data.extend([[i.id, i.position]])
        if data:
            self.board.bus_servo_set_position(msg.duration, data)

    def set_bus_servo_state(self, msg):
        data = []
        servo_id = []
        for i in msg.state:
            if i.present_id:
                if i.present_id[0]:
                    if i.target_id:
                        if i.target_id[0]:
                            self.board.bus_servo_set_id(i.present_id[1], i.target_id[1])
                    if i.position:
                        if i.position[0]:
                            data.extend([[i.present_id[1], i.position[1]]])
                    if i.offset:
                        if i.offset[0]:
                            self.board.bus_servo_set_offset(i.present_id[1], i.offset[1])
                    if i.position_limit:
                        if i.position_limit[0]:
                            self.board.bus_servo_set_angle_limit(i.present_id[1], i.position_limit[1:])
                    if i.voltage_limit:
                        if i.voltage_limit[0]:
                            self.board.bus_servo_set_vin_limit(i.present_id[1], i.voltage_limit[1:])
                    if i.max_temperature_limit:
                        if i.max_temperature_limit[0]:
                            self.board.bus_servo_set_temp_limit(i.present_id[1], i.max_temperature_limit[1])
                    if i.enable_torque:
                        if i.enable_torque[0]:
                            self.board.bus_servo_enable_torque(i.present_id[1], i.enable_torque[1])
                    if i.save_offset:
                        if i.save_offset[0]:
                            self.board.bus_servo_save_offset(i.present_id[1])
                    if i.stop:
                        if i.stop[0]:
                            servo_id.append(i.present_id[1])
        if data != []:
            self.board.bus_servo_set_position(msg.duration, data)
        if servo_id != []:    
            self.board.bus_servo_stop(servo_id)

    def get_bus_servo_state(self, request, response):
        states = []
        for i in request.cmd:
            data = BusServoState()
            if i.get_id:
                state = self.board.bus_servo_read_id(i.id)
                if state is not None:
                    i.id = state[0]
                    data.present_id = state
            if i.get_position:
                state = self.board.bus_servo_read_position(i.id)
                if state is not None:
                    data.position = state
            if i.get_offset:
                state = self.board.bus_servo_read_offset(i.id)
                if state is not None:
                    data.offset = state
            if i.get_voltage:
                state = self.board.bus_servo_read_voltage(i.id)
                if state is not None:
                    data.voltage = state
            if i.get_temperature:
                state = self.board.bus_servo_read_temp(i.id)
                if state is not None:
                    data.temperature = state
            if i.get_position_limit:
                state = self.board.bus_servo_read_angle_limit(i.id)
                if state is not None:
                    data.position_limit = state
            if i.get_voltage_limit:
                state = self.board.bus_servo_read_vin_limit(i.id)
                if state is not None:
                    data.voltage_limit = state
            if i.get_max_temperature_limit:
                state = self.board.bus_servo_read_temp_limit(i.id)
                if state is not None:
                    data.max_temperature_limit = state
            if i.get_torque_state:
                state = self.board.bus_servo_read_torque(i.id)
                if state is not None:
                    data.enable_torque = state
            states.append(data)
        response.state = states
        response.success = True
        return response

    def pub_battery_data(self, pub):
        data = self.board.get_battery()
        if data is not None:
            msg = UInt16()
            msg.data = data
            pub.publish(msg)

    def pub_button_data(self, pub):
        data = self.board.get_button()
        if data is not None:
            key_id, key_event = data
            state_map = {
                PacketReportKeyEvents.KEY_EVENT_PRESSED: 1,
                PacketReportKeyEvents.KEY_EVENT_LONGPRESS: 2,
                PacketReportKeyEvents.KEY_EVENT_LONGPRESS_REPEAT: 3,
                PacketReportKeyEvents.KEY_EVENT_RELEASE_FROM_LP: 4,
                PacketReportKeyEvents.KEY_EVENT_RELEASE_FROM_SP: 0,
                PacketReportKeyEvents.KEY_EVENT_CLICK: 5,
                PacketReportKeyEvents.KEY_EVENT_DOUBLE_CLICK: 6,
                PacketReportKeyEvents.KEY_EVENT_TRIPLE_CLICK: 7,
            }
            state = state_map.get(key_event, -1)

            if state != -1:
                msg = ButtonState()
                msg.id = key_id
                msg.state = state
                pub.publish(msg)
            else:
                self.get_logger().error(f"Unhandled button event: {key_event}")

    def pub_joy_data(self, pub):
        data = self.board.get_gamepad()
        if data is not None:
            msg = Joy()
            msg.axes = data[0]
            msg.buttons = data[1]
            msg.header.stamp = self.clock.now().to_msg()
            pub.publish(msg)

    def pub_sbus_data(self, pub):
        data = self.board.get_sbus()
        if data is not None:
            msg = Sbus()
            msg.channel = data
            msg.header.stamp = self.clock.now().to_msg()
            pub.publish(msg)

    def pub_imu_data(self, pub):
        data = self.board.get_imu()
        if data is not None:
            ax, ay, az, gx, gy, gz = data
            msg = Imu()
            msg.header.frame_id = self.IMU_FRAME
            msg.header.stamp = self.clock.now().to_msg()

            msg.orientation.w = 0.0
            msg.orientation.x = 0.0
            msg.orientation.y = 0.0
            msg.orientation.z = 0.0

            msg.linear_acceleration.x = ax * self.gravity
            msg.linear_acceleration.y = ay * self.gravity
            msg.linear_acceleration.z = az * self.gravity

            msg.angular_velocity.x = math.radians(gx)
            msg.angular_velocity.y = math.radians(gy)
            msg.angular_velocity.z = math.radians(gz)

            msg.orientation_covariance = [0.01, 0.0, 0.0,
                                          0.0, 0.01, 0.0,
                                          0.0, 0.0, 0.01]
            msg.angular_velocity_covariance = [0.01, 0.0, 0.0,
                                              0.0, 0.01, 0.0,
                                              0.0, 0.0, 0.01]
            msg.linear_acceleration_covariance = [0.0004, 0.0, 0.0,
                                                 0.0, 0.0004, 0.0,
                                                 0.0, 0.0, 0.004]
            pub.publish(msg)

def main(args=None):
    if not rclpy.ok():
        rclpy.init(args=args)
    node = None
    try:
        node = RosRobotController('ros_robot_controller')
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.safe_shutdown()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        print('shutdown finish')

if __name__ == '__main__':
    main()
