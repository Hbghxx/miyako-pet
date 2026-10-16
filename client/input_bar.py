"""输入栏（M1.10）：文本输入 + 麦克风按钮 + 采集帧缓冲。

信号：
  text_submitted(str)        —— 回车/发送键
  mic_toggled(bool)          —— 麦克风开/关
  audio_frame(object)        —— 采集帧（int16 numpy，供上层经 WS 上行）
样式极简（M+3.5 settings_window 完整版再统一主题）。
"""
from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import QEvent, Qt, Signal
from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from client.audio_capture import AudioCapture
from client.settings_window import _rewrite_key_at_path, _yaml_scalar
from server.constants import SETTINGS_PATH

logger = logging.getLogger(__name__)


def write_mic_enabled(on: bool,
                      settings_path: Path | None = None) -> None:
    """🎤 对话开关状态持久化（2026-09-12 用户需求）：写 settings.yaml 的
    voice.autostart_mic，行级保注释（启动时据此自动恢复开/关）。"""
    try:
        _rewrite_key_at_path(
            Path(settings_path) if settings_path is not None else SETTINGS_PATH,
            ("voice", "autostart_mic"), _yaml_scalar(bool(on)))
    except Exception:
        logger.warning("麦克风开关状态持久化失败（内存态已生效）：%s", on,
                       exc_info=True)


class InputBar(QWidget):
    text_submitted = Signal(str)
    mic_toggled = Signal(bool)
    audio_frame = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self._grip = QLabel("⠿", self)               # 拖拽把手（2026-09-11 位置记忆）
        self._grip.setCursor(Qt.CursorShape.SizeAllCursor)
        self._grip.setToolTip("拖动移动输入栏")
        self._grip.setFixedWidth(14)
        self._grip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._grip.installEventFilter(self)
        self._drag_offset = None
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("输入文字，或点麦克风说话…")
        self.send_btn = QPushButton("发送", self)
        self.mic_btn = QPushButton("🎤", self)
        self.mic_btn.setCheckable(True)
        layout.addWidget(self._grip)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.send_btn)
        layout.addWidget(self.mic_btn)

        self.edit.returnPressed.connect(self._submit)
        self.send_btn.clicked.connect(self._submit)
        self.mic_btn.toggled.connect(self._on_mic_toggled)

        self._capture = AudioCapture(on_frame=self._on_frame,
                                     notify=lambda msg: logger.warning("%s", msg))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """⠿ 把手拖动窗口（与输入框/按钮的点击互不干扰）。"""
        if obj is self._grip:
            if event.type() == QEvent.Type.MouseButtonPress \
                    and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = (event.globalPosition().toPoint()
                                     - self.pos())
                return True
            if event.type() == QEvent.Type.MouseMove \
                    and self._drag_offset is not None \
                    and (event.buttons() & Qt.MouseButton.LeftButton):
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_offset = None
                return True
        return super().eventFilter(obj, event)

    def set_width(self, width: int) -> None:
        """输入栏宽度（ui_theme.input_bar.width；下限 200 防挤碎按钮）。"""
        self.setMinimumWidth(max(200, int(width)))

    def set_font_size(self, size: int) -> None:
        """输入栏字号（ui_theme.input_bar.font_size），编辑框与按钮整体生效。"""
        from PySide6.QtGui import QFont

        font = QFont()
        font.setPointSize(max(8, int(size)))
        self.setFont(font)

    def _submit(self) -> None:
        text = self.edit.text().strip()
        if not text:
            return
        self.text_submitted.emit(text)
        self.edit.clear()

    def _on_mic_toggled(self, on: bool) -> None:
        if on:
            started = self._capture.start()
            if not started:
                # 降级已提示「禁用语音输入」；按钮回弹（blockSignals 防再入：
                # setChecked(False) 会同步再触发本槽 → AudioEnd 双发）
                self.mic_btn.blockSignals(True)
                self.mic_btn.setChecked(False)
                self.mic_btn.blockSignals(False)
                on = False
        else:
            self._capture.stop()
        self.mic_toggled.emit(on)

    def _on_frame(self, frame: np.ndarray) -> None:
        self.audio_frame.emit(frame)
