"""桌面宠物窗口（M1.2）。

无边框 / 置顶 / Qt.Tool（无任务栏项）/ WA_TranslucentBackground；
30ms QTimer 采样鼠标处「屏幕像素 alpha」：alpha<8 且非拖拽 → 开
WA_TransparentForMouseEvents（逐像素穿透）；拖拽期间强制关穿透。
单实例：ensure_single_instance 经 get_platform().acquire_single_instance_lock
获取 "miyako_single_instance"，失败 → 气泡提示 → sys.exit(0)
（M1.5 气泡组件上线前，提示文案暂由平台对话框承载）。

纯逻辑（穿透判定 / 单实例流程）与 Qt 壳分离，可无窗口单测。
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, Qt, QTimer, Signal  # noqa: F401 (QPoint 供拖拽偏移使用)
from PySide6.QtGui import QCursor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from client.platform.base import PlatformProvider, get_platform
from server.persona import display_name

SINGLE_INSTANCE_MUTEX = "miyako_single_instance"   # 产品级标识：应用级命名，不随角色变（M-CHA 形态铁律）
MOUSE_SAMPLE_INTERVAL_MS = 30
ALPHA_PASS_THRESHOLD = 8  # 屏幕 alpha < 8 视为透明区，允许点击穿透


def should_pass_mouse_events(screen_alpha: int, dragging: bool) -> bool:
    """纯逻辑：透明区且非拖拽 → 穿透。拖拽期间无条件关穿透（跟手优先）。"""
    return screen_alpha < ALPHA_PASS_THRESHOLD and not dragging


def ensure_single_instance(platform: PlatformProvider | None = None) -> None:
    """获取单实例互斥量；失败 → 气泡提示 → sys.exit(0)。

    退出语义在调用方语义层（本函数），平台层只返回 True/False。
    """
    provider = platform or get_platform()
    if not provider.acquire_single_instance_lock(SINGLE_INSTANCE_MUTEX):
        name = display_name()   # 弹窗文案读激活角色展示名；互斥量本身仍是应用级
        provider.show_error_dialog(name, f"{name}已经在运行啦～")
        sys.exit(0)


class PetWindow(QWidget):
    """立绘宿主窗口。M1.3 的 sprite_manager 经 set_sprite 提供像素内容。"""

    moved = Signal()   # 窗口移动（拖拽）后发射——气泡相对跟随（run_client 接线）

    def __init__(self, platform: PlatformProvider | None = None) -> None:
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._platform = platform or get_platform()
        self._sprite: QPixmap | None = None
        self._sprite_image: QImage | None = None   # 逐像素穿透采样用（30ms 定时器热路径）
        self._dragging = False
        self._drag_offset = QPoint(0, 0)  # type: ignore[assignment]
        self._mouse_timer = QTimer(self)
        self._mouse_timer.setInterval(MOUSE_SAMPLE_INTERVAL_MS)
        self._mouse_timer.timeout.connect(self._sample_mouse_alpha)
        self._mouse_timer.start()

    # ---- 内容 ----
    def set_opacity(self, opacity: float) -> None:
        """整窗不透明度（设置窗「界面」实时项，2026-09-12 用户需求）。

        与逐像素 alpha 穿透互不干扰——穿透采样读的是立绘图自身 alpha，
        不经窗口透明度；下限 0.1 防止整只“消失”找不到。
        """
        self.setWindowOpacity(max(0.1, min(1.0, float(opacity))))

    def set_sprite(self, pixmap: QPixmap | None,
                   target_height: int | None = None) -> None:
        """载入立绘；target_height>0 时等比缩放到该显示高度。

        ui_theme.pet_window.base_height（run_client 传入）——产线立绘分辨率大，
        不缩放会占满半屏；0/None = 原始尺寸。
        pixmap=None（素材缺失的降级路径，_load_sprite_pixmap 契约）→ 透明窗口：
        此前直接 pixmap.height() 崩 AttributeError，被 guarded_main 当崩溃弹窗。
        """
        if pixmap is None:
            self._sprite = None
            self._sprite_image = None
            self.resize(1, 1)
            self.update()
            return
        if target_height and pixmap.height() > 0 and pixmap.height() != target_height:
            pixmap = pixmap.scaledToHeight(
                int(target_height), Qt.TransformationMode.SmoothTransformation)
        self._sprite = pixmap
        # QImage 一次性换算缓存：_sprite_alpha_at 在 30ms 鼠标采样定时器里跑，
        # 每帧对大立绘 toImage() 是纯浪费（换图才需重算）
        self._sprite_image = pixmap.toImage()
        self.resize(pixmap.size())
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self._sprite is None:
            return
        with QPainter(self) as painter:
            painter.drawPixmap(0, 0, self._sprite)

    # ---- 逐像素穿透 ----
    def _screen_alpha_at(self, global_pos) -> int:
        """鼠标处屏幕像素 alpha；采样失败按不透明处理（保守，不穿透）。"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return 255
        image = screen.grabWindow(0, global_pos.x(), global_pos.y(), 1, 1).toImage()
        if image.isNull():
            return 255
        return image.pixelColor(0, 0).alpha()

    def _sprite_alpha_at(self, global_pos) -> int:
        """鼠标处在「本窗口立绘」上的 alpha（决定能否开始拖拽）。"""
        if self._sprite is None or self._sprite_image is None:
            return 0
        local = self.mapFromGlobal(global_pos)
        if not (0 <= local.x() < self._sprite_image.width()
                and 0 <= local.y() < self._sprite_image.height()):
            return 0
        return self._sprite_image.pixelColor(local.x(), local.y()).alpha()

    def _sample_mouse_alpha(self) -> None:
        pos = QCursor.pos()
        self.setAttribute(
            Qt.WA_TransparentForMouseEvents,
            should_pass_mouse_events(self._screen_alpha_at(pos), self._dragging))

    # ---- 拖拽 ----
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.LeftButton
                and self._sprite_alpha_at(event.globalPosition().toPoint()) > 0):
            self._dragging = True
            self._drag_offset = self.pos() - event.globalPosition().toPoint()
            # 拖拽期间强制关穿透（plan M1.2 硬性要求）
            self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.move(event.globalPosition().toPoint() + self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False
        event.accept()

    def moveEvent(self, event) -> None:  # noqa: N802
        self.moved.emit()      # 气泡相对跟随（run_client: moved → bubble.set_anchor）
        super().moveEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._mouse_timer.stop()
        super().closeEvent(event)
