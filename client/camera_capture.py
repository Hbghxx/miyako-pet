"""摄像头采集（M-CAM/VIS.2）—— QtMultimedia 默认设备低频抓帧。

零新增依赖（PySide6 自带 QtMultimedia，已验证）。仅当观察循环请求帧时才转换
（``request_frame`` 置位 → 下一帧 videoFrameChanged 落地即出）；无设备/权限 →
``unavailable`` 信号降级，不崩溃（摄像头指示灯亮属诚实行为）。
帧输出为 JPEG 字节，最长边钳制 ``max_side``（与 ScreenShot 同规）。
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QBuffer, QIODevice, QObject, Signal

logger = logging.getLogger(__name__)


def qimage_to_jpeg_bytes(image, max_side: int = 0, quality: int = 80) -> bytes:
    """QImage → JPEG 字节；max_side>0 时等比缩放（只缩不放）。"""
    from PySide6.QtCore import Qt

    img = image
    if max_side and max(img.width(), img.height()) > max_side:
        mode = Qt.TransformationMode.SmoothTransformation
        if img.width() >= img.height():
            img = img.scaledToWidth(int(max_side), mode)
        else:
            img = img.scaledToHeight(int(max_side), mode)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPEG", quality)
    return bytes(buf.data())


class CameraCapture(QObject):
    frame_captured = Signal(bytes)   # JPEG 字节（已钳制尺寸）
    unavailable = Signal(str)        # 无设备/权限/打开失败（降级提示）

    def __init__(self, max_side: int = 1024, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.max_side = max(0, int(max_side))
        self._camera = None
        self._session = None
        self._sink = None
        self._want = False
        self._failed = False

    def start(self) -> bool:
        """打开默认摄像头；返回 False = 不可用（unavailable 信号已发）。"""
        if self._camera is not None:
            return True
        try:
            from PySide6.QtMultimedia import (QCamera, QMediaCaptureSession,
                                              QMediaDevices, QVideoSink)
        except Exception as exc:
            self._fail(f"QtMultimedia 不可用：{exc}")
            return False
        device = QMediaDevices.defaultVideoInput()
        if device is None or device.isNull():
            self._fail("未检测到摄像头")
            return False
        self._camera = QCamera(device)
        self._session = QMediaCaptureSession()
        self._sink = QVideoSink()
        self._session.setCamera(self._camera)
        self._session.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_video_frame)
        # QCamera.start() 是异步的：设备被占用/权限拒绝等真实失败此后才经
        # errorOccurred 到达——不接线则「打开失败必须可见化」对最常见失败不成立
        self._camera.errorOccurred.connect(self._on_camera_error)
        self._camera.start()
        return True

    def _on_camera_error(self, error, error_string: str) -> None:
        if int(error) == 0:      # QCamera.NoError
            return
        self._fail(error_string or f"摄像头错误（{error}）")

    def stop(self) -> None:
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                logger.debug("摄像头停止异常", exc_info=True)
        self._camera = None
        self._session = None
        self._sink = None
        self._want = False

    def request_frame(self) -> None:
        """观察循环到点：请求下一帧（摄像头帧率高于采样率，置位即可）。"""
        self._want = True

    def _fail(self, reason: str) -> None:
        self._failed = True
        logger.warning("摄像头不可用：%s", reason)
        self.unavailable.emit(reason)

    def _on_video_frame(self, frame) -> None:
        if not self._want:
            return
        self._want = False
        image = frame.toImage()
        if image.isNull():
            return
        try:
            data = qimage_to_jpeg_bytes(image, self.max_side)
        except Exception:
            logger.warning("帧转 JPEG 失败", exc_info=True)
            return
        self.frame_captured.emit(data)
