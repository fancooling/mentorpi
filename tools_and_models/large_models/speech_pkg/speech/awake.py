#!/usr/bin/env python3
# encoding: utf-8
# @Author: Aiden
# @Date: 2025/02/21
import re
import json
import time
import queue
import serial
import importlib.util as util

class WonderEchoPro:
    WAKEUP = b'\xaa\x55\x03\x00\xfb'
    SLEEP = b'\xaa\x55\x02\x00\xfb'
    def __init__(self, port):
        self.serialHandle = serial.Serial(None, 115200, serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE, timeout=0.02)
        self.serialHandle.rts = False
        self.serialHandle.dtr = False
        self.serialHandle.setPort(port)
        self.serialHandle.open()

    def start(self):
        while self.serialHandle.in_waiting > 0:  # 只要缓冲区有数据，就一直读取并丢弃
            self.serialHandle.read(self.serialHandle.in_waiting)

    def wakeup(self):
        recv_data = self.detect()
        if recv_data == self.WAKEUP:
            return True
        else:
            return False

    def detect(self):
        return self.serialHandle.read(5)

    def exit(self):
        self.serialHandle.close()

class CircleMic:
    def __init__(self, port='/dev/ttyCH341USB0', awake_word='xiao3 huan4 xiao3 huan4', mic_type='mic6_circle',
                 enable_setting=False):
        self.serialHandle = serial.Serial(None, 115200, serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE, timeout=0.02)
        self.serialHandle.rts = False
        self.serialHandle.dtr = False
        self.serialHandle.setPort(port)
        self.serialHandle.open()
        self.buffer = ""
        self.running = True
        self.key_type = r"{\"code.*?\"}"
        self.pattern = re.compile(r"{\"content.*?aiui_event\"}")
        
        if enable_setting:
            self.switch_mic(mic_type)
            self.set_wakeup_word(awake_word)

    # 麦克风阵列切换
    def switch_mic(self, mic="mic6_circle"):
        # mic：麦克风阵列类型，mic4：线性4麦，mic6：线性6麦， mic6_circle：环形6麦
        param = {
            "type": "switch_mic",
            "content": {
                "mic": "mic6_circle"
            }
        }
        param['content']['mic'] = mic
        header = [0xA5, 0x01, 0x05]
        res = self.send(header, param)
        if res is not None:
            pattern = re.compile(self.key_type)
            m = re.search(pattern, str(res))
            if m is not None:
                m = m.group(0)
                if m is not None:
                    return m

        return False

    # 获取版本信息
    def get_setting(self):
        param = {
            "type": "version"
        }

        header = [0xA5, 0x01, 0x05]
        res = self.send(header, param)
        if res is not None:
            pattern = re.compile(self.key_type)
            m = re.search(pattern, str(res))
            if m is not None:
                m = m.group(0)
                if m is not None:
                    return m

        return False

    # 唤醒词更换（浅定制）
    def set_wakeup_word(self, str_pinyin="xiao3 huan4 xiao3 huan4"):
        # 参数为中文拼音
        # 更多参数请参考https://aiui.xfyun.cn/doc/aiui/3_access_service/access_hardware/r818/protocol.html
        param = {
            "type": "wakeup_keywords",
            "content": {
                "keyword": "xiao3 huan4 xiao3 huan4",
                "threshold": "400"
            }
        }

        param['content']['keyword'] = str_pinyin
        header = [0xA5, 0x01, 0x05]
        print('\033[1;32m%s\033[0m' % 'setting wakeup keywords need about 30s')
        print('\033[1;32m%s\033[0m' % 'setting ......')
        self.send(header, param)
        while time.time() - self.start_time < 30:
            time.sleep(0.1)

    # 计算校验和
    def calculate_checksum(self, bytes_list):
        checksum = sum(bytes_list) & 0xFF
        checksum = (~checksum + 1) & 0xFF
        return checksum

    # 数据串口发送
    def send_data(self, header, args):
        packet = header

        data = bytes(json.dumps(args), encoding="utf8")

        length = len(data)
        low_length = int(length & 0xFF)
        high_length = int(length >> 8)

        packet.extend([low_length, high_length])
        packet.extend([0x00, 0x00])

        packet.extend(data)
        checksum = self.calculate_checksum(packet)
        packet.append(checksum)

        self.serialHandle.write(packet)  # 发送主控消息

    # 发送数据
    def send(self, header, args):
        self.serialHandle.write([0xa5, 0x01, 0x01, 0x04, 0x00, 0x00, 0x00, 0xa5, 0x00, 0x00, 0x00, 0xb0])  # 发送握手请求
        while True:
            result = None
            recv_data = self.serialHandle.read()
            header_ = [b'\xa5', b'\x01', b'\xff']
            if recv_data == header_[0]:
                recv_data = self.serialHandle.read()
                if recv_data == header_[1]:
                    recv_data = self.serialHandle.read()
                    if recv_data == header_[2]:
                        recv_data = self.serialHandle.read(4)
                        self.serialHandle.read((recv_data[1] << 8 | recv_data[0]) + 1)
                        self.send_data(header, args)
                        self.start_time = time.time()
                        break

                    else:  # 没有收到确认
                        recv_data = self.serialHandle.read(4)
                        self.serialHandle.read((recv_data[1] << 8 | recv_data[0]) + 1)

                        time.sleep(0.1)
                        self.serialHandle.write(
                            [0xa5, 0x01, 0x01, 0x04, 0x00, 0x00, 0x00, 0xa5, 0x00, 0x00, 0x00, 0xb0])  # 继续发送发送握手请求

        result = None
        while True:
            recv_data = self.serialHandle.read()
            header_ = [b'\xa5', b'\x01', b'\x04']
            if recv_data == header_[0]:
                recv_data = self.serialHandle.read()
                if recv_data == header_[1]:
                    recv_data = self.serialHandle.read()
                    if recv_data == header_[2]:
                        recv_data = self.serialHandle.read(4)
                        result = self.serialHandle.read((recv_data[1] << 8 | recv_data[0]) + 1)
                        break
        return result

    def val_map(self, x, in_min, in_max, out_min, out_max):
        return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min


    def parse_angle_from_content(self, content_str):
        """
        从content字符串中解析angle值
        :param content_str: content字符串
        :return: angle值，如果解析失败返回None
        """
        try:
            # 尝试解析JSON
            data = json.loads(content_str)
            
            # 获取info字段
            if 'content' in data and 'info' in data['content']:
                info_str = data['content']['info']
                # info是一个JSON字符串，需要再次解析
                info_data = json.loads(info_str)
                
                # 获取angle
                if 'ivw' in info_data and 'angle' in info_data['ivw']:
                    angle = info_data['ivw']['angle']
                    return angle
        except (json.JSONDecodeError, KeyError) as e:
            print(f"解析错误: {e}")
            return None
        
        return None
    
    def find_json_in_buffer(self):
        """
        在缓冲区中查找完整的JSON字符串
        :return: JSON字符串，如果没有找到返回None
        """
        # 查找JSON开始标记
        start = self.buffer.find('{"content":')
        if start == -1:
            start = self.buffer.find('{"type":')
        
        if start != -1:
            # 尝试找到完整的JSON
            brace_count = 0
            in_string = False
            escape = False
            
            for i in range(start, len(self.buffer)):
                char = self.buffer[i]
                
                if escape:
                    escape = False
                    continue
                    
                if char == '\\':
                    escape = True
                    continue
                
                if char == '"':
                    in_string = not in_string
                
                if not in_string:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        
                        if brace_count == 0:
                            # 找到完整的JSON
                            json_str = self.buffer[start:i+1]
                            self.buffer = self.buffer[i+1:]
                            return json_str
        
        return None

    def wakeup(self):
        """
        持续读取串口数据并解析angle
        """
        angle = False
        try:
            # 检查串口是否打开
            if not self.serialHandle.is_open:
                return angle
                
            # 读取串口数据
            if self.serialHandle.in_waiting > 0:
                data = self.serialHandle.read(self.serialHandle.in_waiting)
                try:
                    # 尝试解码为UTF-8
                    decoded = data.decode('utf-8', errors='ignore')
                    self.buffer += decoded
                    
                    # 查找并解析JSON
                    while True:
                        json_str = self.find_json_in_buffer()
                        if json_str is None:
                            break
                        
                        # 解析angle
                        angle = self.parse_angle_from_content(json_str)
                        if angle is not None:
                            angle = self.val_map(angle, 0, 360, 360, 0) + 240  # 和圆形兼容
                            if angle >= 360:
                                angle -= 360
                            return int(angle)
                
                except Exception as e:
                    # 可以选择打印错误信息用于调试
                    # print(f"解析数据时出错: {e}")
                    pass
                        
        except Exception as e:
            # 捕获串口读取异常
            # print(f"串口读取错误: {e}")
            pass
        
        return angle

    # 检测是否唤醒以及唤醒对应角度
    def wakeup1(self):
        angle = False
        recv_data = self.serialHandle.read()
        if recv_data:
            print(recv_data)
        if recv_data == b'\xa5':
            recv_data = self.serialHandle.read()
            if recv_data == b'\x01':
                recv_data = self.serialHandle.read()
                if recv_data == b'\x04':
                    recv_data = self.serialHandle.read(4)
                    result = self.serialHandle.read((recv_data[1] << 8 | recv_data[0]) + 1)
                    if b'content' in result:
                        m = re.search(self.pattern, str(result).replace('\\', ''))
                        if m is not None:
                            m = m.group(0).replace('"{"', '{"').replace('}"', '}')
                            if m is not None:
                                angle = int(json.loads(m)['content']['info']['ivw']['angle'])
                                angle = self.val_map(angle, 0, 360, 360, 0) + 240  # 和圆形兼容
                                if angle >= 360:
                                    angle -= 360
                                return int(angle)
        return angle

    def start(self):
        while self.serialHandle.in_waiting > 0:  # 只要缓冲区有数据，就一直读取并丢弃
            self.serialHandle.read(self.serialHandle.in_waiting)

    def exit(self):
        self.serialHandle.close()


class PiSpeechBoard:
    WAKEUP = b'\xbb\xcc\x03\x00\xfb'
    SLEEP = b'\xbb\xcc\x02\x00\xfb'

    def __init__(self, port='/dev/rrc', use_ros=False):
        from ros_robot_controller.ros_robot_controller_sdk import Board

        self.use_ros = use_ros
        self.data_queue = queue.Queue(maxsize=2)
        if not self.use_ros:
            from ros_robot_controller.ros_robot_controller_sdk import Board
            self.board = Board(device=port)
        else:
            if util.find_spec('rclpy') is not None:
                import rclpy, threading
                from rclpy.executors import SingleThreadedExecutor
                from interfaces.msg import BoardCi1302Frame

                if not rclpy.ok():
                    rclpy.init()
                self.ros_node = rclpy.create_node('pispeech_board_%x' % id(self))
                self.ros_executor = SingleThreadedExecutor()
                self.ros_executor.add_node(self.ros_node)
                self.ros_sub = self.ros_node.create_subscription(BoardCi1302Frame, '/motion/board/ci1302', self.callback, 5)
                self.ros_spin_thread = threading.Thread(target=self.ros_executor.spin, daemon=True)
                self.ros_spin_thread.start()
            elif util.find_spec('rospy') is not None:
                import rospy
                from std_msgs.msg import UInt8MultiArray
                rospy.Subscriber('ros_robot_controller/asr', UInt8MultiArray, self.callback)

    def callback(self, msg):
        if not self.data_queue.empty():
            try:
                self.data_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.data_queue.put_nowait(bytes(msg.data))
        except queue.Empty:
            pass

    def start(self):
        if not self.use_ros:
            self.board.enable_reception()
        else:
            try:
                while True:
                    self.data_queue.get_nowait()
            except queue.Empty:
                pass

    def wakeup(self):
        if not self.use_ros:
            report = self.board.poll_ci1302_report()
            recv_data = b'' if report is None else report.get('frame', b'')
        else:
            try:
                recv_data = self.data_queue.get(block=False)
            except queue.Empty:
                return False
        return recv_data == self.WAKEUP

    def detect(self):
        if not self.use_ros:
            report = self.board.poll_ci1302_report()
            recv_data = b'' if report is None else report.get('frame', b'')
            return recv_data
        try:
            return self.data_queue.get(block=False)
        except queue.Empty:
            return b''

    def exit(self):
        if not self.use_ros:
            self.board.enable_reception(False)


if __name__ == '__main__':
    kws = CircleMic('/dev/ring_mic', 'xiao3 huan4 xiao3 huan4', 'mic6_circle', False)
    # kws.start()
    print('start')
    while True:
        if kws.wakeup():
            print('wakeup')
        # time.sleep(0.01)
