import os
import sys
import yaml
import time
import threading
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                             QLabel, QLineEdit, QPushButton, QMessageBox, QDoubleSpinBox)
from PyQt5.QtCore import Qt, QTimer
from ros_robot_controller_sdk import Board

class DeviationCorrectorApp(QWidget):
    def __init__(self):
        super().__init__()
        self.yaml_file = "robot_correction_factors.yaml"
        self.board = Board()  # 创建机器人控制板实例
        self.machine_type = os.getenv('MACHINE_TYPE', 'MentorPi_Tank')
        self.initUI()
        self.load_factors_from_yaml()
        
        # 初始化机器人
        self.init_robot()
        
        # 设置定时器用于持续发送电机命令
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.send_motor_commands)
        self.timer.start(100)  # 每100ms发送一次命令
        
        # 键盘控制状态
        self.key_states = {
            Qt.Key_W: False,  # 前进
            Qt.Key_S: False,  # 后退
            Qt.Key_A: False,  # 左转
            Qt.Key_D: False   # 右转
        }
    
    def init_robot(self):
        """初始化机器人设置"""
        self.board.enable_reception()
        for i in range(2):
            if 'Tank' in self.machine_type:
                self.board.set_motor_type(0x00)
            elif 'Acker' in self.machine_type:
                self.board.set_motor_type(0x01)
            time.sleep(0.1)
        print("Robot initialized")
        
        # 当前控制参数
        self.base_speed = 0.6  # 基础速度值
        self.left_motor_id = 1  # 左电机ID
        self.right_motor_id = 3  # 右电机ID
    
    def initUI(self):
        self.setWindowTitle('Track straight deviation adjustment tool')
        self.setGeometry(300, 300, 500, 400)

        main_layout = QVBoxLayout()
        main_layout.setSpacing(15)

        # 校正因子设置组
        correction_group = QGroupBox("(Correction factor setting)")
        correction_layout = QVBoxLayout()
        
        title_label = QLabel('Adjustment')
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("font-size: 14px; font-weight: bold; margin-bottom: 10px;")
        correction_layout.addWidget(title_label)

        # 左履带校正因子
        h_layout_left = QHBoxLayout()
        h_layout_left.addWidget(QLabel('<b>left:</b>'))
        self.left_factor_input = QDoubleSpinBox(self)
        self.left_factor_input.setRange(0.5, 1.5)
        self.left_factor_input.setSingleStep(0.01)
        self.left_factor_input.setValue(1.0)
        self.left_factor_input.setDecimals(3)
        self.left_factor_input.valueChanged.connect(self.factor_changed)
        h_layout_left.addWidget(self.left_factor_input)
        correction_layout.addLayout(h_layout_left)

        # 右履带校正因子
        h_layout_right = QHBoxLayout()
        h_layout_right.addWidget(QLabel('<b>right:</b>'))
        self.right_factor_input = QDoubleSpinBox(self)
        self.right_factor_input.setRange(0.5, 1.5)
        self.right_factor_input.setSingleStep(0.01)
        self.right_factor_input.setValue(1.0)
        self.right_factor_input.setDecimals(3)
        self.right_factor_input.valueChanged.connect(self.factor_changed)
        h_layout_right.addWidget(self.right_factor_input)
        correction_layout.addLayout(h_layout_right)

        # 操作按钮
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        self.save_button = QPushButton(' Save YAML', self)
        self.save_button.setToolTip('Save YAML File')
        self.save_button.clicked.connect(self.save_factors_to_yaml)
        button_layout.addWidget(self.save_button)

        self.load_button = QPushButton('From YAML Import', self)
        self.load_button.setToolTip('From YAML Import the Parm')
        self.load_button.clicked.connect(self.load_factors_from_yaml)
        button_layout.addWidget(self.load_button)

        correction_layout.addLayout(button_layout)
        correction_group.setLayout(correction_layout)
        main_layout.addWidget(correction_group)

        # 机器人控制组
        control_group = QGroupBox("ConTrol (WASDS)")
        control_layout = QVBoxLayout()
        
        # 基础速度设置
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(QLabel('<b>Speed Valu:</b>'))
        self.speed_input = QDoubleSpinBox(self)
        self.speed_input.setRange(0.1, 1.0)
        self.speed_input.setSingleStep(0.1)
        self.speed_input.setValue(0.6)
        self.speed_input.setDecimals(2)
        self.speed_input.valueChanged.connect(self.speed_changed)
        speed_layout.addWidget(self.speed_input)
        control_layout.addLayout(speed_layout)
        
        # 方向控制按钮
        direction_layout = QVBoxLayout()
        
        # 前进按钮
        forward_layout = QHBoxLayout()
        forward_layout.addStretch()
        self.forward_button = QPushButton('Forwared (W)', self)
        self.forward_button.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.forward_button.setFixedSize(120, 50)
        self.forward_button.pressed.connect(lambda: self.set_direction('forward'))
        self.forward_button.released.connect(self.stop)
        forward_layout.addWidget(self.forward_button)
        forward_layout.addStretch()
        direction_layout.addLayout(forward_layout)
        
        # 左右和后退按钮
        middle_layout = QHBoxLayout()
        self.left_button = QPushButton('turn Left (A)', self)
        self.left_button.setFixedSize(100, 40)
        self.left_button.pressed.connect(lambda: self.set_direction('left'))
        self.left_button.released.connect(self.stop)
        middle_layout.addWidget(self.left_button)
        
        self.backward_button = QPushButton('Back (S)', self)
        self.backward_button.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.backward_button.setFixedSize(120, 50)
        self.backward_button.pressed.connect(lambda: self.set_direction('backward'))
        self.backward_button.released.connect(self.stop)
        middle_layout.addWidget(self.backward_button)
        
        self.right_button = QPushButton('Right (D)', self)
        self.right_button.setFixedSize(100, 40)
        self.right_button.pressed.connect(lambda: self.set_direction('right'))
        self.right_button.released.connect(self.stop)
        middle_layout.addWidget(self.right_button)
        direction_layout.addLayout(middle_layout)
        
        control_layout.addLayout(direction_layout)
        control_group.setLayout(control_layout)
        main_layout.addWidget(control_group)
        
        # 停止按钮
        self.stop_button = QPushButton('Stop (Space)', self)
        self.stop_button.setStyleSheet("background-color: #FF9999; font-weight: bold; font-size: 16px;")
        self.stop_button.setFixedHeight(60)
        self.stop_button.clicked.connect(self.stop)
        main_layout.addWidget(self.stop_button)

        self.setLayout(main_layout)
        
        # 当前运动状态
        self.direction = None
    
    def keyPressEvent(self, event):
        """处理键盘按下事件"""
        key = event.key()
        
        # 检查是否是WASD键
        if key in [Qt.Key_W, Qt.Key_S, Qt.Key_A, Qt.Key_D]:
            self.key_states[key] = True
            self.update_direction_from_keys()
        elif key == Qt.Key_Space:
            self.stop()
        else:
            super().keyPressEvent(event)
    
    def keyReleaseEvent(self, event):
        """处理键盘释放事件"""
        key = event.key()
        
        if key in [Qt.Key_W, Qt.Key_S, Qt.Key_A, Qt.Key_D]:
            self.key_states[key] = False
            self.update_direction_from_keys()
        else:
            super().keyReleaseEvent(event)
    
    def update_direction_from_keys(self):
        """根据当前按下的键更新运动方向"""
        w_pressed = self.key_states[Qt.Key_W]
        s_pressed = self.key_states[Qt.Key_S]
        a_pressed = self.key_states[Qt.Key_A]
        d_pressed = self.key_states[Qt.Key_D]
        
        # 确定运动方向优先级：前进/后退 > 左转/右转
        if w_pressed and s_pressed:
            # 同时按下W和S，停止
            self.direction = None
        elif w_pressed:
            if a_pressed and d_pressed:
                self.direction = 'forward'  # 只前进
            elif a_pressed:
                self.direction = 'forward_left'  # 前进+左转
            elif d_pressed:
                self.direction = 'forward_right'  # 前进+右转
            else:
                self.direction = 'forward'  # 前进
        elif s_pressed:
            if a_pressed and d_pressed:
                self.direction = 'backward'  # 只后退
            elif a_pressed:
                self.direction = 'backward_left'  # 后退+左转
            elif d_pressed:
                self.direction = 'backward_right'  # 后退+右转
            else:
                self.direction = 'backward'  # 后退
        elif a_pressed and d_pressed:
            self.direction = None  # 同时按下A和D，停止
        elif a_pressed:
            self.direction = 'left'  # 左转
        elif d_pressed:
            self.direction = 'right'  # 右转
        else:
            self.direction = None  # 没有键被按下，停止
        
        # 更新按钮状态
        self.update_button_states()
    
    def update_button_states(self):
        """根据当前方向更新按钮按下状态"""
        self.forward_button.setDown(self.direction in ['forward', 'forward_left', 'forward_right'])
        self.backward_button.setDown(self.direction in ['backward', 'backward_left', 'backward_right'])
        self.left_button.setDown(self.direction in ['left', 'forward_left', 'backward_left'])
        self.right_button.setDown(self.direction in ['right', 'forward_right', 'backward_right'])
    
    def factor_changed(self):
        """校正因子改变时更新显示"""
        left = self.left_factor_input.value()
        right = self.right_factor_input.value()
        print(f"校正因子更新: 左={left:.3f}, 右={right:.3f}")
    
    def speed_changed(self):
        """速度值改变时更新显示"""
        self.base_speed = self.speed_input.value()
        print(f"基础速度更新: {self.base_speed:.2f}")
    
    def set_direction(self, direction):
        """设置机器人运动方向"""
        self.direction = direction
        print(f"方向设置: {direction}")
        self.update_button_states()
    
    def stop(self):
        """停止机器人运动"""
        self.direction = None
        print("机器人停止")
        self.update_button_states()
    
    def send_motor_commands(self):
        """发送电机控制命令（由定时器定期调用）"""
        if self.direction is None:
            # 停止所有电机
            motor_commands = [
                [1, 0.0],
                [2, 0.0],
                [3, 0.0],
                [4, 0.0]
            ]
            self.board.set_motor_speed(motor_commands)
            return

        # 获取当前校正因子
        left_factor = self.left_factor_input.value()
        right_factor = self.right_factor_input.value()
        
        # 计算实际速度（基础速度 * 校正因子）
        left_speed = self.base_speed * left_factor
        right_speed = self.base_speed * right_factor
        
        # 根据方向调整速度
        if self.direction == 'forward':
            # 前进：左轮负速度，右轮正速度
            left_speed = -left_speed
            right_speed = right_speed
        elif self.direction == 'backward':
            # 后退：左轮正速度，右轮负速度
            left_speed = left_speed
            right_speed = -right_speed
        elif self.direction == 'left':
            # 左转：左轮正速度，右轮正速度
            left_speed = left_speed
            right_speed = right_speed
        elif self.direction == 'right':
            # 右转：左轮负速度，右轮负速度
            left_speed = -left_speed
            right_speed = -right_speed
        elif self.direction == 'forward_left':
            # 前进+左转：左轮速度减小
            left_speed = -left_speed * 0.7  # 前进速度的70%
            right_speed = right_speed
        elif self.direction == 'forward_right':
            # 前进+右转：右轮速度减小
            left_speed = -left_speed
            right_speed = right_speed * 0.7  # 前进速度的70%
        elif self.direction == 'backward_left':
            # 后退+左转：左轮速度减小
            left_speed = left_speed * 0.7  # 后退速度的70%
            right_speed = -right_speed
        elif self.direction == 'backward_right':
            # 后退+右转：右轮速度减小
            left_speed = left_speed
            right_speed = -right_speed * 0.7  # 后退速度的70%
        
        # 创建电机命令（只控制1号和3号电机，其他设为0）
        motor_commands = [
            [self.left_motor_id, left_speed],
            [2, 0.0],  # 未使用的电机设为0
            [self.right_motor_id, right_speed],
            [4, 0.0]   # 未使用的电机设为0
        ]

        # 发送命令
        self.board.set_motor_speed(motor_commands)
    
    def save_factors_to_yaml(self):
        try:
            factors = {
                'left_correction_factor': self.left_factor_input.value(),
                'right_correction_factor': self.right_factor_input.value()
            }

            # 原子写入（先写临时文件再重命名）
            temp_file = self.yaml_file + '.tmp'
            with open(temp_file, 'w') as f:
                yaml.dump(factors, f)
            
            if os.path.exists(self.yaml_file):
                os.replace(temp_file, self.yaml_file)
            else:
                os.rename(temp_file, self.yaml_file)
            QMessageBox.information(self, 'Save',f'Save successful')
        except Exception as e:
            QMessageBox.critical(self, 'Save Fail', f'Save Fail: {str(e)}')

    def load_factors_from_yaml(self):
        """从 YAML 文件加载校正因子并更新界面。"""
        try:
            with open(self.yaml_file, 'r') as f:
                factors = yaml.safe_load(f)
            if factors and isinstance(factors, dict):
                left_factor = factors.get('left_correction_factor', 1.0)
                right_factor = factors.get('right_correction_factor', 1.0)
                self.left_factor_input.setValue(float(left_factor))
                self.right_factor_input.setValue(float(right_factor))
                # QMessageBox.information(self, '加载成功', f'校正因子已从:\n{self.yaml_file}\n成功加载。')
            else:
                QMessageBox.warning(self, 'Loading failed', f'The file {self.yaml_file} The file {self.yaml_file} is empty or malformed. Please check the file contents.')
        except FileNotFoundError:
            QMessageBox.warning(self, 'File does not exist', f'Calibration File {self.yaml_file} Does not exist. \nDefault values ​​will be used. The first save will create this file.')
            self.left_factor_input.setValue(1.0)
            self.right_factor_input.setValue(1.0)
        except yaml.YAMLError as e:
            QMessageBox.critical(self, 'YAML parsing errors', f'Unable to parse YAML file:{e}\nPlease ensure that the file format is correct.')
        except Exception as e:
            QMessageBox.critical(self, 'Loading failed', f'An unknown error occurred while loading the file:{e}')
    
    def closeEvent(self, event):
        """窗口关闭时停止所有电机"""
        self.stop()
        self.timer.stop()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = DeviationCorrectorApp()
    ex.show()
    sys.exit(app.exec_())