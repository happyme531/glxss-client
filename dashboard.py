"""Native PyQt6 dashboard. Runs on Linux with direct local libusb access."""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QPointF,
    QProcess,
    QRectF,
    QSettings,
    QStandardPaths,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from frame_transport import PreviewBuffer
from interaction import CompassCalibration, magnetic_heading
from playback import PlaybackGuard

ROOT = Path(__file__).resolve().parent
COLORS = ["#65e4bb", "#70a9ff", "#e5b86e"]
STYLE = """
QMainWindow, QWidget#root { background: #10151c; color: #e5edf6; }
QWidget { color: #e5edf6; font-family: 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif; font-size: 13px; }
QFrame#card { background: #19212b; border: 1px solid #2a3441; border-radius: 13px; }
QLabel { background: transparent; border: none; }
QLabel#muted { color: #8c9baa; }
QLabel#eyebrow { color: #6bdfb9; font-size: 11px; font-weight: 600; }
QLabel#value { color: #f3f7fc; font-size: 27px; font-weight: 600; }
QLabel#title { font-size: 19px; font-weight: 600; }
QLabel#badge { color: #75e6c2; background: #17372e; border: 1px solid #285443; border-radius: 13px; padding: 5px 13px; }
QPushButton { background: #263240; color: #e5edf6; border: 1px solid #3a4858; border-radius: 7px; padding: 8px 16px; }
QPushButton:hover { background: #334357; border-color: #65798f; }
QPushButton:pressed { background: #1b2835; }
QPushButton:disabled { color: #627080; background: #202934; border-color: #2a3543; }
QPushButton#primary { background: #65e4bb; color: #10251e; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #8af0ce; }
QPushButton#primary:disabled { background: #304c43; color: #76998b; }
QComboBox { background: #202b37; border: 1px solid #3a4858; border-radius: 7px; padding: 8px 12px; min-width: 130px; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox QAbstractItemView { background: #202b37; selection-background-color: #34495d; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; color: #8c9baa; padding: 12px 24px; margin-right: 8px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #77e8c3; border-bottom: 2px solid #65e4bb; }
QTabBar::tab:hover { color: #e5edf6; }
QPlainTextEdit { background: #111921; color: #c7d6e7; border: 1px solid #303d4c; border-radius: 8px; padding: 12px; selection-background-color: #365b6b; }
QPlainTextEdit#log { background: #111921; color: #94a8b8; font-family: 'DejaVu Sans Mono', monospace; font-size: 11px; padding: 6px; }
QSlider::groove:horizontal { height: 5px; border-radius: 2px; background: #354353; }
QSlider::sub-page:horizontal { background: #65e4bb; border-radius: 2px; }
QSlider::handle:horizontal { background: #c9f9e9; width: 15px; margin: -5px 0; border-radius: 7px; }
QCheckBox { spacing: 10px; }
QProgressBar { background: #101921; border: 1px solid #304050; border-radius: 5px; min-height: 16px; color: #d8e8ee; text-align: center; }
QProgressBar::chunk { background: #418c75; border-radius: 4px; }
QScrollBar:vertical { background: #19212b; width: 8px; }
QScrollBar::handle:vertical { background: #3a4858; min-height: 30px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def label(text, name=None):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 17, 20, 17)
    layout.setSpacing(12)
    return frame, layout


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


class ImageView(QWidget):
    def __init__(self, placeholder="等待摄像头画面"):
        super().__init__()
        self.pixmap = QPixmap()
        self.placeholder = placeholder
        self.setMinimumSize(240, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_image(self, image):
        self.pixmap = QPixmap.fromImage(image)
        self.update()

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0b1118"))
        if not self.pixmap.isNull():
            fitted = self.pixmap.size().scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio
            )
            target = QRectF(
                (self.width() - fitted.width()) / 2,
                (self.height() - fitted.height()) / 2,
                fitted.width(),
                fitted.height(),
            )
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))
        else:
            painter.setPen(QColor("#56697c"))
            center = QPointF(self.width() / 2, self.height() / 2 - 26)
            painter.drawEllipse(center, 27, 27)
            painter.drawEllipse(center, 10, 10)
            painter.drawText(
                self.rect().adjusted(0, 75, 0, 0),
                Qt.AlignmentFlag.AlignCenter,
                self.placeholder,
            )


class Trace(QWidget):
    def __init__(self, title, unit, axes, capacity=150):
        super().__init__()
        self.title, self.unit, self.axes = title, unit, axes
        self.samples = deque(maxlen=capacity)
        self.setMinimumHeight(150)

    def append(self, values):
        self.samples.append(values)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("#d5e2ef"))
        p.drawText(0, 16, self.title)
        p.setPen(QColor("#788fa3"))
        p.drawText(self.width() - 65, 16, self.unit)
        area = QRectF(0, 38, self.width(), self.height() - 66)
        p.setPen(QPen(QColor("#2b3946"), 1, Qt.PenStyle.DashLine))
        for fraction in (0.25, 0.5, 0.75):
            y = area.y() + area.height() * fraction
            p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        if not self.samples:
            return
        extent = (
            max(0.05, max(abs(value) for values in self.samples for value in values))
            * 1.2
        )
        for axis, color in enumerate(COLORS[: len(self.axes)]):
            path = QPainterPath()
            for i, values in enumerate(self.samples):
                x = area.left() + i / (self.samples.maxlen - 1) * area.width()
                y = area.center().y() - values[axis] / extent * area.height() / 2
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            p.setPen(QPen(QColor(color), 1.7))
            p.drawPath(path)
            p.drawText(
                int(axis * self.width() / len(self.axes)),
                self.height() - 3,
                f"{self.axes[axis]}  {self.samples[-1][axis]:+.2f}",
            )


class Attitude(QWidget):
    def __init__(self):
        super().__init__()
        self.pitch = self.roll = 0.0
        self.setMinimumHeight(160)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, 78)
        radius = 64
        p.save()
        clip = QPainterPath()
        clip.addEllipse(center, radius, radius)
        p.setClipPath(clip)
        p.translate(center)
        p.rotate(-self.roll)
        offset = max(-55, min(55, self.pitch * 1.4))
        p.fillRect(QRectF(-180, -180, 360, 180 + offset), QColor("#263f58"))
        p.fillRect(QRectF(-180, offset, 360, 180), QColor("#244339"))
        p.setPen(QPen(QColor("#8db4c3"), 1))
        p.drawLine(QPointF(-100, offset), QPointF(100, offset))
        for y in (-40, -20, 20, 40):
            p.drawLine(QPointF(-14, y + offset), QPointF(14, y + offset))
        p.restore()
        p.setPen(QPen(QColor("#547181"), 2))
        p.drawEllipse(center, radius, radius)
        p.setPen(QPen(QColor("#80ebc5"), 3))
        p.drawLine(QPointF(center.x() - 28, 78), QPointF(center.x() - 8, 78))
        p.drawLine(QPointF(center.x() + 8, 78), QPointF(center.x() + 28, 78))
        p.drawEllipse(center, 3, 3)
        p.setPen(QColor("#9cafbf"))
        p.drawText(
            QRectF(0, 146, self.width(), 20),
            Qt.AlignmentFlag.AlignCenter,
            f"俯仰 {self.pitch:+.1f}°     横滚 {self.roll:+.1f}°",
        )


class CompassDial(QWidget):
    def __init__(self):
        super().__init__()
        self.heading = None
        self.setMinimumSize(220, 230)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2 - 12)
        radius = min(self.width(), self.height()) * 0.35
        p.setPen(QPen(QColor("#3c5568"), 2))
        p.setBrush(QColor("#12212d"))
        p.drawEllipse(center, radius, radius)
        for i in range(36):
            angle = math.radians(i * 10 - 90)
            outer, inner = radius - 7, radius - (17 if i % 9 == 0 else 11)
            p.setPen(QPen(QColor("#7897a9" if i % 9 == 0 else "#3c5568"), 1))
            p.drawLine(
                QPointF(
                    center.x() + inner * math.cos(angle),
                    center.y() + inner * math.sin(angle),
                ),
                QPointF(
                    center.x() + outer * math.cos(angle),
                    center.y() + outer * math.sin(angle),
                ),
            )
        p.setPen(QColor("#9db2c3"))
        for name, dx, dy in (
            ("0°", 0, -radius - 14),
            ("90°", radius + 23, 0),
            ("180°", 0, radius + 17),
            ("270°", -radius - 23, 0),
        ):
            p.drawText(
                QRectF(center.x() + dx - 25, center.y() + dy - 10, 50, 20),
                Qt.AlignmentFlag.AlignCenter,
                name,
            )
        if self.heading is not None:
            p.save()
            p.translate(center)
            p.rotate(self.heading)
            path = QPainterPath()
            path.moveTo(0, -radius + 26)
            path.lineTo(-10, 14)
            path.lineTo(0, 6)
            path.lineTo(10, 14)
            path.closeSubpath()
            p.fillPath(path, QColor("#70e8bf"))
            p.restore()
        p.setPen(QColor("#e8f6f6"))
        p.setFont(QFont("sans-serif", 17))
        p.drawText(
            QRectF(0, self.height() - 30, self.width(), 28),
            Qt.AlignmentFlag.AlignCenter,
            f"{self.heading:05.1f}°" if self.heading is not None else "—",
        )


class Dashboard(QMainWindow):
    def __init__(self, diagnostics=None, autoconnect=True):
        super().__init__()
        self.setWindowTitle("GLXSS Lab · 眼镜控制台")
        self.resize(1380, 960)
        self.diagnostics = diagnostics
        self.process = None
        self.audio_process = None
        self.audio_buffer = bytearray()
        self.audio_connected = self.mic_active = self.record_active = False
        self.last_recording = None
        self.audio_controls = []
        self.settings = QSettings("glxss", "lab")
        self.calibration = CompassCalibration()
        self.calibration_path = (
            Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppConfigLocation
                )
            )
            / "compass.json"
        )
        self.calibration.load(self.calibration_path)
        self.last_sensor = None
        self.heading = None
        self.source_fps = self.source_mbps = 0.0
        self.physical_presses = 0
        self.last_capture_stats = {}
        self.last_mirror_stats = {}
        self.connected = self.camera_running = False
        self.camera_audio_suspended = False
        self.playback_guard = PlaybackGuard(self.send, self.audio_write)
        self.buffer = bytearray()
        self.preview_buffer = PreviewBuffer()
        self.preview_sequence = 0
        self.preview_size = None
        self.frames = self.sensor_count = self.key_count = 0
        self.frame_times = deque(maxlen=45)
        self.last_frame = QImage()
        self.device_info = {}
        self.fps = 0.0
        self.closing = False
        self.controls = []
        self.build()
        self.enable_controls(False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(2000)
        if autoconnect:
            QTimer.singleShot(450, self.connect_device)

    def build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 22, 28, 17)
        layout.setSpacing(15)
        header = QHBoxLayout()
        brand = QVBoxLayout()
        brand.setSpacing(2)
        brand.addWidget(label("GLXSS  /  LAB", "eyebrow"))
        title = label("眼镜控制台", "title")
        title.setStyleSheet("font-size:26px;")
        brand.addWidget(title)
        header.addLayout(brand)
        header.addSpacing(24)
        header.addWidget(label("让旧硬件，重新看见。", "muted"))
        header.addStretch()
        self.badge = label("○  等待连接", "badge")
        header.addWidget(self.badge)
        header.addSpacing(12)
        self.connect_button = button("连接眼镜", self.toggle_connection)
        header.addWidget(self.connect_button)
        layout.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.live_tab(), "实时工作台")
        self.tabs.addTab(self.sensors_tab(), "罗盘与按键")
        self.tabs.addTab(self.audio_tab(), "音频")
        self.tabs.addTab(self.display_tab(), "屏幕投送")
        self.tabs.addTab(self.research_tab(), "功能与研究")
        layout.addWidget(self.tabs, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(label("设备事件", "eyebrow"))
        bottom.addStretch()
        self.event_count = label("0 次按键变化", "muted")
        bottom.addWidget(self.event_count)
        layout.addLayout(bottom)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(350)
        self.log_view.setFixedHeight(88)
        layout.addWidget(self.log_view)
        self.footer = label("本机 USB · 等待设备信息", "muted")
        self.footer.setStyleSheet("font-size:11px;")
        layout.addWidget(self.footer)

    def live_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 17, 0, 0)
        outer.setSpacing(15)
        row = QHBoxLayout()
        row.setSpacing(15)
        camera_card, camera = card()
        bar = QHBoxLayout()
        bar.addWidget(label("第一视角", "title"))
        bar.addStretch()
        self.video_status = label("CAMERA / STANDBY", "eyebrow")
        bar.addWidget(self.video_status)
        camera.addLayout(bar)
        self.video = ImageView()
        self.video.setMinimumHeight(310)
        camera.addWidget(self.video, 1)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(["640 × 480", "1280 × 720", "1920 × 1080"])
        self.mode.setToolTip("切换分辨率前先暂停预览。实际采集帧率显示在右侧。")
        controls.addWidget(self.mode)
        self.fps_mode = QComboBox()
        self.fps_mode.addItems(["设备最高帧率", "30 FPS"])
        self.mode.setCurrentIndex(int(self.settings.value("camera_mode", 0)))
        self.fps_mode.setCurrentIndex(int(self.settings.value("camera_fps", 0)))
        self.mode.currentIndexChanged.connect(
            lambda index: self.settings.setValue("camera_mode", index)
        )
        self.fps_mode.currentIndexChanged.connect(
            lambda index: self.settings.setValue("camera_fps", index)
        )
        controls.addWidget(self.fps_mode)
        self.camera_button = button("开启摄像头", self.toggle_camera, True)
        self.capture_button = button("保存照片", self.save_photo)
        controls.addWidget(self.camera_button)
        controls.addWidget(self.capture_button)
        controls.addStretch()
        self.fps_label = label("软件不限速", "muted")
        controls.addWidget(self.fps_label)
        camera.addLayout(controls)
        self.controls.extend([self.mode, self.fps_mode, self.camera_button])
        row.addWidget(camera_card, 3)
        sidebar = QVBoxLayout()
        sidebar.setSpacing(13)
        attitude_card, attitude = card()
        attitude_bar = QHBoxLayout()
        attitude_bar.addWidget(label("头部姿态", "title"))
        attitude_bar.addStretch()
        attitude_bar.addWidget(label("重力估算", "muted"))
        attitude.addLayout(attitude_bar)
        self.attitude = Attitude()
        self.attitude.setToolTip(
            "俯仰：抬头为正、低头为负。由重力估算，快速运动时可能有偏差。"
        )
        attitude.addWidget(self.attitude)
        attitude.addWidget(label("俯仰：抬头＋ / 低头−", "muted"))
        sidebar.addWidget(attitude_card, 1)
        stats_card, stats = card()
        self.lux = label("—", "value")
        self.temperature = label("—", "value")
        stats_grid = QGridLayout()
        stats_grid.addWidget(label("环境光 / lux", "muted"), 0, 0)
        stats_grid.addWidget(label("传感器温度 / °C", "muted"), 0, 1)
        stats_grid.addWidget(self.lux, 1, 0)
        stats_grid.addWidget(self.temperature, 1, 1)
        stats.addLayout(stats_grid)
        self.compass = label("罗盘 · 等待读数", "muted")
        stats.addWidget(self.compass)
        self.key_label = label("按键 · 等待事件", "muted")
        stats.addWidget(self.key_label)
        sidebar.addWidget(stats_card)
        row.addLayout(sidebar, 1)
        outer.addLayout(row, 1)
        traces_card, traces_layout = card()
        traces = QHBoxLayout()
        traces.setSpacing(26)
        self.acc_trace = Trace("加速度", "g", ["X", "Y", "Z"])
        self.gyro_trace = Trace("角速度", "°/s", ["X", "Y", "Z"])
        self.angle_trace = Trace("姿态变化", "°", ["俯仰", "横滚"])
        for trace in (self.acc_trace, self.gyro_trace, self.angle_trace):
            traces.addWidget(trace, 1)
        traces_layout.addLayout(traces)
        outer.addWidget(traces_card)
        return page

    def sensors_tab(self):
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 17, 0, 0)
        row.setSpacing(18)
        compass_card, compass = card()
        compass.addWidget(label("磁场与航向", "title"))
        compass.addWidget(label("传感器坐标下的磁航向 · 可设为相对方向", "muted"))
        self.compass_dial = CompassDial()
        compass.addWidget(self.compass_dial, 1)
        self.magnetic_trace = Trace("三轴磁场", "原始值", ["X", "Y", "Z"])
        compass.addWidget(self.magnetic_trace)
        self.calibration_status = label(
            "已载入校准"
            if self.calibration.valid
            else "尚未校准：航向仅供相对方向测试",
            "muted",
        )
        self.calibration_status.setWordWrap(True)
        compass.addWidget(self.calibration_status)
        buttons = QHBoxLayout()
        self.calibrate_button = button("开始三轴校准", self.toggle_calibration)
        self.zero_button = button("当前方向设为 0°", self.zero_heading)
        buttons.addWidget(self.calibrate_button)
        buttons.addWidget(self.zero_button)
        compass.addLayout(buttons)
        axis_row = QHBoxLayout()
        axis_row.addWidget(label("眼镜前向轴", "muted"))
        self.forward_axis = QComboBox()
        self.forward_axis.addItems(["+Z（默认）", "+X", "−Z", "−X"])
        self.forward_axis.setCurrentIndex(int(self.settings.value("forward_axis", 0)))
        self.forward_axis.currentIndexChanged.connect(self.change_forward_axis)
        axis_row.addWidget(self.forward_axis)
        self.tilt_compensation = QCheckBox("倾斜补偿")
        self.tilt_compensation.setChecked(True)
        axis_row.addWidget(self.tilt_compensation)
        compass.addLayout(axis_row)
        note = label(
            "校准时缓慢向各方向转动眼镜。附近金属会影响磁场；尚未验证板载坐标与头部方向的安装偏差。",
            "muted",
        )
        note.setWordWrap(True)
        compass.addWidget(note)
        row.addWidget(compass_card, 3)
        key_card, key = card()
        key.addWidget(label("机身按键", "title"))
        key.addWidget(label("唯一已确认按键 · 0x71", "eyebrow"))
        self.button_state = label("已松开", "value")
        key.addWidget(self.button_state)
        self.gesture_label = label(
            "短按：松开时触发\n长按：按住至少 0.8 秒后松开", "muted"
        )
        key.addWidget(self.gesture_label)
        key.addSpacing(12)
        choices = ["不执行动作", "拍照", "录音起停", "投送当前文字", "切换预览"]
        key.addWidget(label("短按动作", "eyebrow"))
        self.short_action = QComboBox()
        self.short_action.addItems(choices)
        self.short_action.setCurrentText(
            str(self.settings.value("short_action", "拍照"))
        )
        self.short_action.currentTextChanged.connect(
            lambda text: self.settings.setValue("short_action", text)
        )
        key.addWidget(self.short_action)
        key.addWidget(label("长按动作", "eyebrow"))
        self.long_action = QComboBox()
        self.long_action.addItems(choices)
        self.long_action.setCurrentText(
            str(self.settings.value("long_action", "录音起停"))
        )
        self.long_action.currentTextChanged.connect(
            lambda text: self.settings.setValue("long_action", text)
        )
        key.addWidget(self.long_action)
        key.addSpacing(12)
        self.press_count_label = label("本次连接按下 0 次", "muted")
        key.addWidget(self.press_count_label)
        self.key_history = QPlainTextEdit()
        self.key_history.setReadOnly(True)
        self.key_history.setMaximumBlockCount(100)
        self.key_history.setPlaceholderText("按下眼镜按键，这里会显示动作与持续时间。")
        key.addWidget(self.key_history, 1)
        row.addWidget(key_card, 2)
        return page

    def audio_tab(self):
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 17, 0, 0)
        row.setSpacing(18)
        mic_card, mic = card()
        mic_header = QHBoxLayout()
        mic_header.addWidget(label("眼镜麦克风", "title"))
        mic_header.addStretch()
        self.audio_badge = label("等待连接", "eyebrow")
        mic_header.addWidget(self.audio_badge)
        mic.addLayout(mic_header)
        mic.addWidget(label("44.1 kHz / 16 bit · L 后麦克风 / R 前麦克风", "muted"))
        mic.addSpacing(12)
        self.level_bars, self.level_labels = [], []
        for channel in ("后麦克风 · L", "前麦克风 · R"):
            level_row = QHBoxLayout()
            level_row.addWidget(label(channel, "eyebrow"))
            meter = QProgressBar()
            meter.setRange(0, 90)
            meter.setValue(0)
            meter.setTextVisible(False)
            level_row.addWidget(meter, 1)
            value = label("— dBFS", "muted")
            value.setMinimumWidth(95)
            level_row.addWidget(value)
            self.level_bars.append(meter)
            self.level_labels.append(value)
            mic.addLayout(level_row)
        self.audio_trace = Trace("麦克风电平", "dBFS", ["后 L", "前 R"])
        mic.addWidget(self.audio_trace)
        mic_buttons = QHBoxLayout()
        self.mic_button = button("开启麦克风", self.toggle_microphone)
        self.record_button = button("开始录音", self.toggle_recording, True)
        mic_buttons.addWidget(self.mic_button)
        mic_buttons.addWidget(self.record_button)
        mic.addLayout(mic_buttons)
        self.record_status = label("录音未开始", "value")
        self.record_status.setStyleSheet("font-size:21px;")
        mic.addWidget(self.record_status)
        self.record_path_label = label(
            "录音保存到 captures/audio/，停止后可直接回放。", "muted"
        )
        self.record_path_label.setWordWrap(True)
        self.record_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        mic.addWidget(self.record_path_label)
        self.replay_button = button("在眼镜上回放最近录音", self.replay_recording)
        self.replay_button.setEnabled(False)
        mic.addWidget(self.replay_button)
        mic.addStretch()
        self.audio_diagnostics = label("USB 音频尚未连接", "muted")
        self.audio_diagnostics.setWordWrap(True)
        mic.addWidget(self.audio_diagnostics)
        row.addWidget(mic_card, 3)
        speaker_card, speaker = card()
        speaker.addWidget(label("扬声器与增益", "title"))
        self.mixer_widgets = {}
        for target, title in (("speaker", "扬声器音量"), ("microphone", "麦克风增益")):
            speaker.addWidget(label(title, "eyebrow"))
            value = label("等待设备范围", "muted")
            speaker.addWidget(value)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.sliderReleased.connect(
                lambda name=target, widget=slider: self.audio_send(
                    "volume", target=name, percent=widget.value()
                )
            )
            speaker.addWidget(slider)
            mute = QCheckBox("静音" if target == "speaker" else "麦克风静音")
            mute.toggled.connect(
                lambda enabled, name=target: self.audio_send(
                    "mute", target=name, enabled=enabled
                )
            )
            speaker.addWidget(mute)
            self.mixer_widgets[target] = (slider, value, mute)
            self.audio_controls.extend((slider, mute))
            speaker.addSpacing(14)
        self.test_tone_button = button(
            "播放低音量测试音", lambda: self.audio_send("tone")
        )
        self.wav_button = button("在眼镜上播放 WAV…", self.play_wav)
        self.stop_play_button = button("停止播放", lambda: self.audio_send("play_stop"))
        speaker.addWidget(self.test_tone_button)
        speaker.addWidget(self.wav_button)
        speaker.addWidget(self.stop_play_button)
        self.pause_camera_for_audio = QCheckBox("播放音频时暂停摄像头（避免爆音）")
        self.pause_camera_for_audio.setChecked(
            self.settings.value("pause_camera_for_audio", True, type=bool)
        )
        self.pause_camera_for_audio.setToolTip(
            "播放前停止摄像头 USB 采集和视频回传，结束后恢复原分辨率、帧率和预览状态。"
        )
        self.playback_guard.set_enabled(self.pause_camera_for_audio.isChecked())
        self.pause_camera_for_audio.toggled.connect(self.change_audio_camera_guard)
        speaker.addWidget(self.pause_camera_for_audio)
        self.play_status = label("扬声器空闲", "muted")
        self.play_status.setWordWrap(True)
        speaker.addWidget(self.play_status)
        speaker.addStretch()
        speaker.addWidget(button("重新连接音频", self.connect_audio))
        hint = label(
            "音频直接传输到眼镜。麦克风电平默认关闭，点击开启或录音后开始采集。",
            "muted",
        )
        hint.setWordWrap(True)
        speaker.addWidget(hint)
        row.addWidget(speaker_card, 2)
        self.audio_controls.extend(
            [
                self.mic_button,
                self.record_button,
                self.test_tone_button,
                self.wav_button,
                self.stop_play_button,
            ]
        )
        for widget in self.audio_controls:
            widget.setEnabled(False)
        return page

    def toggle_calibration(self):
        if self.calibration.collecting:
            try:
                self.calibration.finish()
                self.calibration.save(self.calibration_path)
                self.heading = None
                self.calibrate_button.setText("重新三轴校准")
                self.calibration_status.setText(
                    "校准已保存 · 已修正三轴中心偏移与量程差异"
                )
                self.log("罗盘三轴校准已保存")
            except ValueError as error:
                self.calibration_status.setText(str(error))
        else:
            self.calibration.start()
            self.calibrate_button.setText("完成并保存校准")
            self.calibration_status.setText("正在采样：请缓慢向各方向转动眼镜。")

    def zero_heading(self):
        if self.last_sensor is None:
            return
        value = self.compute_heading(self.last_sensor)
        if value is not None:
            self.calibration.zero = value
            self.calibration.save(self.calibration_path)
            self.heading = None
            self.log("当前磁航向已设为相对方向 0°")

    def change_forward_axis(self, index):
        self.settings.setValue("forward_axis", index)
        self.heading = None
        self.calibration.zero = 0.0
        self.calibration.save(self.calibration_path)

    def compute_heading(self, sensor):
        forward = [(0, 0, 1), (1, 0, 0), (0, 0, -1), (-1, 0, 0)][
            self.forward_axis.currentIndex()
        ]
        gravity = sensor["acc_g"] if self.tilt_compensation.isChecked() else (0, 1, 0)
        return magnetic_heading(
            self.calibration.correct(sensor["compass_raw"]), gravity, forward
        )

    def perform_key_action(self, action):
        if action == "拍照":
            self.save_photo()
        elif action == "录音起停":
            self.toggle_recording()
        elif action == "投送当前文字":
            self.send("display_text", text=self.text_editor.toPlainText())
        elif action == "切换预览":
            self.toggle_camera()

    def connect_audio(self):
        if (
            self.audio_process
            and self.audio_process.state() != QProcess.ProcessState.NotRunning
        ):
            return
        self.audio_buffer.clear()
        process = self.audio_process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.readyReadStandardOutput.connect(self.read_audio_output)
        process.readyReadStandardError.connect(
            lambda: self.log(
                bytes(process.readAllStandardError()).decode(errors="replace")[-800:]
            )
        )
        process.finished.connect(self.audio_finished)
        process.errorOccurred.connect(
            lambda error: self.log("音频工作进程错误：" + str(error))
        )
        self.audio_badge.setText("正在连接音频")
        process.start(sys.executable, ["-u", str(ROOT / "audio_worker.py")])

    def audio_send(self, op, **data):
        if (
            self.audio_connected
            and self.audio_process
            and self.audio_process.state() == QProcess.ProcessState.Running
        ):
            if op in ("tone", "play"):
                self.playback_guard.play(op, **data)
            elif op == "play_stop":
                self.playback_guard.stop()
            else:
                self.audio_write(op, **data)
        else:
            self.log("音频未连接，请在音频页重新连接。")

    def audio_write(self, op, **data):
        if (
            self.audio_process
            and self.audio_process.state() == QProcess.ProcessState.Running
        ):
            self.audio_process.write(
                (json.dumps({"op": op, **data}, ensure_ascii=False) + "\n").encode()
            )

    def change_audio_camera_guard(self, enabled):
        self.settings.setValue("pause_camera_for_audio", enabled)
        self.playback_guard.set_enabled(enabled)

    def read_audio_output(self):
        self.audio_buffer.extend(bytes(self.audio_process.readAllStandardOutput()))
        while b"\n" in self.audio_buffer:
            line, _, rest = self.audio_buffer.partition(b"\n")
            self.audio_buffer = bytearray(rest)
            try:
                self.handle(json.loads(line))
            except Exception as error:
                self.log("处理音频消息失败：" + str(error))

    def update_mixer(self, mixer):
        for target, values in mixer.items():
            slider, value, mute = self.mixer_widgets[target]
            slider.setValue(values["percent"])
            value.setText(
                f"{values['db']:+.1f} dB  ·  范围 {values['minimum_db']:+.1f} 至 {values['maximum_db']:+.1f} dB"
            )
            mute.blockSignals(True)
            mute.setChecked(values["muted"])
            mute.blockSignals(False)

    def toggle_microphone(self):
        self.audio_send("capture_stop" if self.mic_active else "capture_start")

    def toggle_recording(self):
        self.audio_send("record_stop" if self.record_active else "record_start")

    def replay_recording(self):
        if self.last_recording:
            self.audio_send("play", path=str(self.last_recording))

    def play_wav(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "在眼镜上播放 WAV", str(ROOT / "captures"), "PCM WAV (*.wav)"
        )
        if path:
            self.audio_send("play", path=path)

    def audio_finished(self, code, _):
        self.audio_connected = self.mic_active = self.record_active = False
        self.playback_guard.audio_disconnected()
        self.audio_badge.setText("音频已断开" if code == 0 else "音频连接中断")
        self.mic_button.setText("开启麦克风")
        self.record_button.setText("开始录音")
        for widget in self.audio_controls:
            widget.setEnabled(False)
        self.log("音频工作进程已退出" + (f"（{code}）" if code else ""))
        if self.closing:
            QTimer.singleShot(0, self.close)

    def display_tab(self):
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 17, 0, 0)
        row.setSpacing(18)
        preview_card, preview = card()
        preview.addWidget(label("眼镜屏幕预览", "title"))
        preview.addWidget(label("640 × 400 · 非透明显示屏，黑色显示为黑色", "muted"))
        self.screen_preview = ImageView("投送文字或图片后显示预览")
        preview.addWidget(self.screen_preview, 1)
        self.display_status = label("等待投送", "muted")
        preview.addWidget(self.display_status)
        row.addWidget(preview_card, 3)
        control_card, controls = card()
        controls.addWidget(label("把内容带到眼前", "title"))
        controls.addWidget(label("文字卡片", "eyebrow"))
        self.text_editor = QPlainTextEdit(
            "你好，GLXSS！\n摄像头与传感器已解锁。\n现在开始探索。"
        )
        self.text_editor.setMinimumHeight(155)
        controls.addWidget(self.text_editor)
        send_text = button(
            "投送文字",
            lambda: self.send("display_text", text=self.text_editor.toPlainText()),
            True,
        )
        controls.addWidget(send_text)
        send_image = button("选择并投送图片…", self.send_image)
        controls.addWidget(send_image)
        controls.addSpacing(10)
        controls.addWidget(label("显示控制", "eyebrow"))
        brightness_row = QHBoxLayout()
        brightness_row.addWidget(label("背光原始值", "muted"))
        brightness_row.addStretch()
        self.brightness_value = label("10")
        brightness_row.addWidget(self.brightness_value)
        controls.addLayout(brightness_row)
        self.brightness = QSlider(Qt.Orientation.Horizontal)
        self.brightness.setRange(0, 254)
        self.brightness.setValue(10)
        self.brightness.valueChanged.connect(
            lambda value: self.brightness_value.setText(str(value))
        )
        controls.addWidget(self.brightness)
        apply_brightness = button(
            "应用背光", lambda: self.send("brightness", value=self.brightness.value())
        )
        controls.addWidget(apply_brightness)
        power = QHBoxLayout()
        screen_on = button("亮屏", lambda: self.send("lcd_power", enabled=True))
        screen_off = button("息屏", lambda: self.send("lcd_power", enabled=False))
        power.addWidget(screen_on)
        power.addWidget(screen_off)
        controls.addLayout(power)
        controls.addSpacing(8)
        self.mirror = QCheckBox("将摄像头画面实时回传眼镜（不限速）")
        self.mirror.toggled.connect(
            lambda enabled: self.send("mirror", enabled=enabled)
        )
        controls.addWidget(self.mirror)
        hint = label("先在实时工作台开启摄像头。", "muted")
        hint.setWordWrap(True)
        controls.addWidget(hint)
        controls.addStretch()
        self.controls.extend(
            [
                send_text,
                send_image,
                apply_brightness,
                screen_on,
                screen_off,
                self.mirror,
            ]
        )
        row.addWidget(control_card, 2)
        return page

    def research_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 17, 0, 0)
        layout.setSpacing(16)
        status_card, status = card()
        status.addWidget(label("功能验证地图", "title"))
        entries = [
            ("摄像头", "已实测", "640×480 彩色预览、拍照；更高分辨率待逐项验证"),
            ("显示屏", "已实测", "640×400 图像传输；文字、图片与回传功能可在面板操作"),
            (
                "惯性与光照",
                "已实测",
                "加速度、角速度、环境光；姿态由重力估算，不含绝对航向",
            ),
            ("按键", "已实测", "0x71：按下 1、松开 0；短按/长按可绑定拍照、录音等动作"),
            (
                "罗盘",
                "已读到变化",
                "三轴磁场曲线、中心与量程校准、倾斜补偿磁航向及相对归零",
            ),
            (
                "音频",
                "已接入",
                "USB 麦克风电平、PCM WAV 录音与回放、扬声器音量/静音、麦克风增益",
            ),
            (
                "眼镜端 AI",
                "协议研究中",
                "已发现 Laffe 模型加载入口；尚未验证自定义模型在这副眼镜上运行",
            ),
        ]
        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(18)
        for row, (name, state, description) in enumerate(entries):
            grid.addWidget(label(name), row, 0)
            state_label = label(state)
            state_label.setStyleSheet(
                "color:" + ("#6be0b8" if state == "已实测" else "#d9b36e") + ";"
            )
            grid.addWidget(state_label, row, 1)
            detail = label(description, "muted")
            detail.setWordWrap(True)
            grid.addWidget(detail, row, 2)
        grid.setColumnStretch(2, 1)
        status.addLayout(grid)
        layout.addWidget(status_card)
        device_card, device = card()
        device.addWidget(label("设备信息", "title"))
        self.device_details = label(
            "连接后显示固件版本、传感器芯片与 USB 接口信息。", "muted"
        )
        self.device_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.device_details.setWordWrap(True)
        device.addWidget(self.device_details)
        layout.addWidget(device_card)
        layout.addStretch()
        return page

    def log(self, message):
        self.log_view.appendPlainText(
            datetime.now().strftime("%H:%M:%S") + "  " + message
        )

    def enable_controls(self, enabled):
        for widget in self.controls:
            widget.setEnabled(enabled)
        self.capture_button.setEnabled(enabled and not self.last_frame.isNull())

    def toggle_connection(self):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            self.send("quit")
        else:
            self.connect_device()

    def connect_device(self):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            return
        self.buffer.clear()
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(ROOT))
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.finished.connect(self.backend_finished)
        self.process.errorOccurred.connect(
            lambda error: self.log("USB 工作进程错误：" + str(error))
        )
        self.badge.setText("○  正在连接")
        self.connect_button.setText("断开连接")
        self.log("启动本机 USB 工作进程…")
        self.process.start(
            sys.executable,
            [
                "-u",
                str(ROOT / "bridge.py"),
                "--preview-buffer",
                self.preview_buffer.path,
            ],
        )

    def send(self, op, **data):
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.process.write(
                (json.dumps({"op": op, **data}, ensure_ascii=False) + "\n").encode()
            )
        else:
            self.log("设备未连接")

    def read_output(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            try:
                self.handle(json.loads(line))
            except Exception as error:
                self.log("处理设备消息失败：" + str(error))

    def read_error(self):
        text = (
            bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
        )
        if text:
            self.log(text[-1200:])

    def handle(self, event):
        kind = event["type"]
        if kind == "connected":
            first = not self.connected
            self.connected = True
            self.playback_guard.set_video_available(True)
            self.device_info = info = event["info"]
            self.badge.setText("●  G25 已连接")
            self.enable_controls(True)
            self.brightness.setValue(info["lcd"]["brightness"])
            self.footer.setText(
                f"{info['firmware']}   ·   2e09:0030   ·   {info['usb_speed']}   ·   7 个接口   ·   本机 libusb"
            )
            self.device_details.setText(
                f"固件：{info['firmware']}\n显示：{info['lcd']['width']} × {info['lcd']['height']}，旋转 {info['lcd']['rotation']}°\n"
                f"传感器芯片 ID：光照 0x{info['sensors']['light']['chip_id']:x} / 罗盘 0x{info['sensors']['compass']['chip_id']:x} / IMU 0x{info['sensors']['imu']['chip_id']:x}\n"
                "USB 访问与图形界面均在本机运行。USB 挂接和权限由系统环境提供。"
            )
            if first:
                self.toggle_camera()
                self.connect_audio()
        elif kind == "frame":
            if "sequence" in event:
                frame = self.preview_buffer.read(self.preview_sequence)
                if frame is None:
                    return
                self.preview_sequence = frame["sequence"]
                self.preview_pixels = frame["pixels"]
                image = QImage(
                    self.preview_pixels,
                    frame["width"],
                    frame["height"],
                    frame["width"] * 4,
                    QImage.Format.Format_RGB32,
                )
            else:
                image = QImage.fromData(base64.b64decode(event["jpeg"]))
            if image.isNull():
                raise ValueError("Invalid JPEG frame")
            self.last_frame = image
            self.video.set_image(image)
            self.frames += 1
            self.frame_times.append(time.monotonic())
            self.capture_button.setEnabled(True)
            if len(self.frame_times) > 1:
                self.fps = (len(self.frame_times) - 1) / (
                    self.frame_times[-1] - self.frame_times[0]
                )
            self.fps_label.setText(
                f"采集 {self.source_fps:.1f} / 显示 {self.fps:.1f} FPS"
            )
            self.video_status.setText(
                f"● LIVE  /  {event['width']} × {event['height']}"
            )
        elif kind == "sensor":
            data = event["data"]
            self.last_sensor = data
            self.sensor_count += 1
            self.lux.setText(str(data["lux"]))
            temp = data["temperature_c"]
            self.temperature.setText(f"{temp:.1f}" if temp is not None else "—")
            self.attitude.pitch, self.attitude.roll = (
                data["pitch_deg"],
                data["roll_deg"],
            )
            self.attitude.update()
            self.acc_trace.append(data["acc_g"])
            self.gyro_trace.append(data["gyro_dps"])
            self.angle_trace.append([data["pitch_deg"], data["roll_deg"]])
            raw = data["compass_raw"]
            self.magnetic_trace.append(raw)
            self.calibration.add(raw)
            value = self.compute_heading(data)
            if value is not None:
                value = (value - self.calibration.zero) % 360
                self.heading = (
                    value
                    if self.heading is None
                    else (
                        self.heading + ((value - self.heading + 180) % 360 - 180) * 0.25
                    )
                    % 360
                )
            else:
                self.heading = None
            self.compass_dial.heading = self.heading
            self.compass_dial.update()
            heading_text = f"{self.heading:.1f}°" if self.heading is not None else "—"
            self.compass.setText(
                f"磁航向 {heading_text}  ·  "
                + ("已校准" if self.calibration.valid else "未校准")
            )
            if self.calibration.collecting:
                spans = self.calibration.spans()
                self.calibration_status.setText(
                    f"已采 {self.calibration.samples} 组 · 三轴覆盖 X {spans[0]:.0f} / Y {spans[1]:.0f} / Z {spans[2]:.0f}\n请继续向各方向缓慢转动，完成后保存。"
                )
        elif kind == "key":
            self.key_count += 1
            pressed = event["phase"] == "press"
            self.button_state.setText("● 按下" if pressed else "已松开")
            self.physical_presses = event["presses"]
            self.press_count_label.setText(f"本次连接按下 {self.physical_presses} 次")
            self.key_label.setText("机身按键 · " + ("按下" if pressed else "松开"))
            self.event_count.setText(f"{self.key_count} 次按键变化")
            if pressed:
                self.log("机身按键按下 · 0x71")
            else:
                gesture = "长按" if event["gesture"] == "long" else "短按"
                action = (
                    self.long_action.currentText()
                    if event["gesture"] == "long"
                    else self.short_action.currentText()
                )
                message = f"{gesture} {event['duration_ms']} ms → {action}"
                self.gesture_label.setText("最近动作：" + message)
                self.key_history.appendPlainText(
                    datetime.now().strftime("%H:%M:%S") + "  " + message
                )
                self.log("机身按键：" + message)
                self.perform_key_action(action)
        elif kind == "camera_stats":
            self.last_capture_stats = event
            self.source_fps, self.source_mbps = event["source_fps"], event["mbps"]
            self.fps_label.setText(
                f"采集 {self.source_fps:.1f} / 显示 {self.fps:.1f} FPS"
            )
            self.fps_label.setToolTip(
                f"USB {self.source_mbps:.1f} MB/s · 已收 {event['received']} 帧 · 界面跳过 {event['preview_skipped']} 帧 · 软件不限速"
            )
        elif kind == "camera":
            self.camera_running = event["running"]
            self.camera_audio_suspended = event.get("audio_suspended", False)
            self.camera_button.setText(
                "暂停预览" if self.camera_running else "开启预览"
            )
            self.camera_button.setEnabled(
                self.connected and not self.camera_audio_suspended
            )
            self.mode.setEnabled(
                not self.camera_running
                and self.connected
                and not self.camera_audio_suspended
            )
            self.fps_mode.setEnabled(
                not self.camera_running
                and self.connected
                and not self.camera_audio_suspended
            )
            if self.camera_running:
                self.frame_times.clear()
                self.video_status.setText("● CAMERA / STARTING")
                stream = event.get("stream", {})
                self.log(
                    f"摄像头已开启 · 请求 {stream.get('requested_fps', '—')} FPS · 软件不限速。"
                )
            else:
                if self.camera_audio_suspended:
                    self.video_status.setText("播放音频 · 摄像头已暂停")
                    self.camera_button.setText("音频播放中")
                    self.fps_label.setText("音频结束后自动恢复")
                    self.log("音频保护：摄像头 USB 采集和视频回传已暂停。")
                else:
                    self.video_status.setText(
                        "预览暂停 · 摄像头仍在采集"
                        if event.get("capturing")
                        else "CAMERA / STANDBY"
                    )
                    self.fps_label.setText("软件不限速")
        elif kind == "audio_connected":
            self.audio_connected = True
            self.audio_badge.setText("● USB 音频已连接")
            for widget in self.audio_controls:
                widget.setEnabled(True)
            self.replay_button.setEnabled(self.last_recording is not None)
            self.update_mixer(event["info"]["mixer"])
            self.audio_diagnostics.setText(
                "直接 USB 音频 · 44.1 kHz · L 后麦克风 / R 前麦克风"
            )
            self.log("USB 音频已连接，麦克风保持关闭，等待操作。")
        elif kind == "audio_mixer":
            self.update_mixer(event["mixer"])
        elif kind == "microphone":
            self.mic_active = event["active"]
            self.mic_button.setText("关闭麦克风" if self.mic_active else "开启麦克风")
            if not self.mic_active:
                for meter in self.level_bars:
                    meter.setValue(0)
        elif kind == "audio_level":
            for meter, value, rms in zip(
                self.level_bars, self.level_labels, event["rms_db"]
            ):
                meter.setValue(round(rms + 90))
                value.setText(f"{rms:.1f} dBFS")
            self.audio_trace.append(event["rms_db"])
            if self.record_active:
                self.record_status.setText(f"● REC  {event['record_seconds']:.1f} s")
            self.audio_diagnostics.setText(
                f"已收到 PCM {event['bytes'] / 1024:.0f} KiB · 包异常 {event['errors']}"
            )
        elif kind == "recording":
            self.record_active = event["active"]
            self.record_button.setText(
                "停止并保存录音" if self.record_active else "开始录音"
            )
            self.record_path_label.setText(event["path"])
            if self.record_active:
                self.record_status.setText("● REC  0.0 s")
                self.replay_button.setEnabled(False)
                self.log("开始眼镜麦克风录音")
            else:
                self.last_recording = Path(event["path"])
                self.replay_button.setEnabled(True)
                self.record_status.setText(f"已保存 {event['seconds']:.1f} s")
                self.log(f"录音已保存：{event['path']}（{event['seconds']:.1f} 秒）")
        elif kind == "playback":
            self.play_status.setText(
                ("正在播放：" if event["active"] else "播放结束：") + event["title"]
            )
            self.log(self.play_status.text())
            if not event["active"]:
                self.playback_guard.complete(event.get("request_id"))
        elif kind == "playback_stopped":
            self.playback_guard.complete(event.get("request_id"))
        elif kind == "display":
            self.display_status.setText(event["message"])
            self.screen_preview.set_image(
                QImage.fromData(base64.b64decode(event["preview"]))
            )
            self.log(event["message"])
        elif kind == "photo":
            self.log(
                f"照片已保存：{event['path']}（{event['width']} × {event['height']}）"
            )
        elif kind == "mirror":
            self.mirror.blockSignals(True)
            self.mirror.setChecked(event["enabled"])
            self.mirror.blockSignals(False)
            self.log(
                "摄像头画面回传眼镜已"
                + ("开启 · 软件不限速" if event["enabled"] else "关闭")
            )
        elif kind == "mirror_stats":
            self.last_mirror_stats = event
            if event["enabled"]:
                if event.get("audio_suspended"):
                    self.display_status.setText(
                        "播放音频 · 视频回传已暂停，结束后自动恢复"
                    )
                else:
                    self.display_status.setText(
                        f"USB 送屏 {event['fps']:.1f} FPS · {event['mbps']:.1f} MB/s · 单帧传输 {event['transfer_ms']:.1f} ms · 排队至发送完成 {event['age_ms']:.1f} ms"
                    )
                self.display_status.setToolTip(
                    "这里统计的是 USB 帧发送速率；显示面板的物理刷新率尚未测量。"
                )
        elif kind == "lcd":
            self.brightness.setValue(event["info"]["brightness"])
        elif kind == "log":
            self.log(event["message"])
        elif kind in ("error", "fatal"):
            self.log(f"{event.get('op', '连接')}：{event['message']}")
            if event.get("op") in ("tone", "play"):
                self.playback_guard.complete(event.get("request_id"))
            elif event.get("op") == "audio_hold":
                self.playback_guard.suspension_failed()
                self.log("未能确认摄像头停流，已取消本次待播放音频。")
            if kind == "fatal":
                self.badge.setText("○  连接失败")
        elif kind == "response" and event["op"] == "audio_hold":
            self.playback_guard.acknowledge(event["enabled"])
        elif kind == "response" and event["op"] == "camera_start":
            self.camera_button.setEnabled(True)

    def toggle_camera(self):
        if self.camera_audio_suspended:
            return
        if self.camera_running:
            self.send("preview_pause")
        else:
            mode = ["640x480", "1280x720", "1920x1080"][self.mode.currentIndex()]
            self.send(
                "camera_start",
                mode=mode,
                fps=None if self.fps_mode.currentIndex() == 0 else 30,
            )

    def save_photo(self):
        if self.last_frame.isNull():
            return
        folder = ROOT / "captures"
        folder.mkdir(exist_ok=True)
        path = folder / (
            "glxss-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".png"
        )
        self.send("save_photo", path=str(path))

    def send_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择投送图片", str(ROOT), "Images (*.png *.jpg *.jpeg *.bmp *.webp)"
        )
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            self.log("无法读取图片")
            return
        from PyQt6.QtCore import QBuffer, QIODevice

        data = QBuffer()
        data.open(QIODevice.OpenModeFlag.WriteOnly)
        image = image.scaled(
            1280,
            800,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        image.save(data, "PNG")
        self.send("display_image", data=base64.b64encode(bytes(data.data())).decode())

    def backend_finished(self, code, _):
        self.connected = self.camera_running = False
        self.camera_audio_suspended = False
        self.playback_guard.close()
        self.enable_controls(False)
        self.badge.setText("○  已断开" if code == 0 else "○  连接中断")
        self.connect_button.setText("重新连接")
        self.camera_button.setText("开启摄像头")
        self.video_status.setText("CAMERA / OFFLINE")
        self.log("USB 工作进程已退出" + (f"（{code}）" if code else ""))
        if self.audio_connected:
            self.audio_send("quit")
        if self.closing:
            QTimer.singleShot(0, self.close)

    def tick(self):
        if self.connected:
            ratio = self.video.devicePixelRatioF()
            size = (
                min(1280, max(240, round(self.video.width() * ratio))),
                min(800, max(180, round(self.video.height() * ratio))),
            )
            if size != self.preview_size:
                self.preview_size = size
                self.send("preview_size", width=size[0], height=size[1])
        if self.diagnostics:
            state = {
                "connected": self.connected,
                "camera_running": self.camera_running,
                "frames": self.frames,
                "sensors": self.sensor_count,
                "keys": self.key_count,
                "fps": round(self.fps, 2),
                "updated": time.time(),
                "firmware": self.device_info.get("firmware"),
                "usb_speed": self.device_info.get("usb_speed"),
                "source_fps": self.source_fps,
                "source_mbps": self.source_mbps,
                "capture_stats": self.last_capture_stats,
                "mirror_stats": self.last_mirror_stats,
                "heading": self.heading,
                "audio_connected": self.audio_connected,
                "camera_audio_suspended": self.camera_audio_suspended,
                "pause_camera_for_audio": self.pause_camera_for_audio.isChecked(),
                "playback_request": self.playback_guard.current,
                "preview_size": self.preview_size,
                "microphone_active": self.mic_active,
                "recording": self.record_active,
                "log": self.log_view.toPlainText().splitlines()[-12:],
            }
            self.diagnostics.parent.mkdir(parents=True, exist_ok=True)
            self.diagnostics.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def closeEvent(self, event):
        running = [
            process
            for process in (self.process, self.audio_process)
            if process and process.state() != QProcess.ProcessState.NotRunning
        ]
        if running:
            event.ignore()
            if not self.closing:
                self.closing = True
                self.playback_guard.close()
                for process in running:
                    process.write(b'{"op":"quit"}\n')
                    process.closeWriteChannel()
                QTimer.singleShot(6000, self.stop_stalled_worker)
        else:
            self.preview_buffer.close()
            event.accept()

    def stop_stalled_worker(self):
        for process in (self.process, self.audio_process):
            if process and process.state() != QProcess.ProcessState.NotRunning:
                self.log("工作进程未及时结束，正在终止。")
                process.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument("--no-connect", action="store_true")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("GLXSS Lab")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = Dashboard(args.diagnostics, not args.no_connect)
    import signal

    signal.signal(signal.SIGTERM, lambda *_: window.close())
    signal.signal(signal.SIGINT, lambda *_: window.close())
    available = app.primaryScreen().availableGeometry()
    window.resize(min(1380, available.width() - 70), min(960, available.height() - 70))
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
