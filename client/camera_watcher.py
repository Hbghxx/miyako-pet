"""摄像头观察循环（M-CAM/VIS.6）—— 到点采样 → CameraFrame 上行。

源头约束（plan M-CAM 硬约束的客户端半边）：
  - **enabled 才有 QTimer 采集**（关闭=连摄像头都不开）；
  - **DND 源头停发**：tick 时 DND 开着 → 不请求帧、不上传；
  - ws 未连接 → 不采集（断连不发）；
  - 间隔钳制下限 15s（防手改配置打爆 VLM）。
开关持久化：``write_camera_enabled`` 行级改写 settings.yaml 的
camera.enabled（保注释，与 switcher.write_active_package_id 同工艺）。
"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

from PySide6.QtCore import QObject, QTimer

from server.constants import SETTINGS_PATH
from server.protocol import CameraFrame

logger = logging.getLogger(__name__)

MIN_INTERVAL_S = 15


def write_camera_enabled(enabled: bool,
                         settings_path: Path | None = None) -> None:
    """行级改写 settings.yaml camera.enabled（保注释；节缺失则追加）。"""
    path = settings_path or SETTINGS_PATH
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    out: list[str] = []
    in_camera = False
    replaced = False
    section_exists = False
    for line in text.splitlines():
        if re.match(r"^camera\s*:", line):
            in_camera = True
            section_exists = True
            out.append(line)
            continue
        if re.match(r"^\S", line):
            in_camera = False
        m = (re.match(r"^(\s+)enabled\s*:[^#]*(#.*)?$", line)
             if in_camera else None)
        if m:
            comment = (m.group(2) or "").rstrip()
            out.append(f"{m.group(1)}enabled: {str(enabled).lower()}"
                       + (f"    {comment}" if comment else ""))
            replaced = True
            continue
        out.append(line)
    if not replaced:
        addition = f"  enabled: {str(enabled).lower()}"
        if section_exists:
            for i in range(len(out) - 1, -1, -1):
                if re.match(r"^camera\s*:", out[i]):
                    out.insert(i + 1, addition)
                    break
        else:
            if out and out[-1].strip():
                out.append("")
            out += ["camera:", addition]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    from server.constants import invalidate_text_cache

    invalidate_text_cache(path)   # 读缓存失效契约：读后写立即可见
    logger.info("camera.enabled 已写入 %s：%s", path.name, enabled)


class CameraWatcher(QObject):
    """采样循环：QTimer 到点 → capture.request_frame() → 帧上行。"""

    def __init__(self, ws, capture, *,
                 interval_s: float = 45,
                 enabled: bool = False,
                 dnd_getter=None,
                 connected_getter=None,
                 notify=None) -> None:
        super().__init__()
        self._ws = ws
        self._capture = capture
        self._dnd_getter = dnd_getter or (lambda: False)
        self._connected_getter = connected_getter or (
            lambda: bool(getattr(ws, "_engine", None)
                         and ws._engine._connected))
        self._notify = notify or (lambda msg: logger.info("%s", msg))
        self._timer = QTimer(self)
        self._timer.setInterval(max(MIN_INTERVAL_S, int(interval_s)) * 1000)
        self._timer.timeout.connect(self._tick)
        capture.frame_captured.connect(self._on_frame)
        capture.unavailable.connect(self._on_capture_failed)
        if enabled:
            self.set_enabled(True)

    @property
    def enabled(self) -> bool:
        return self._timer.isActive()

    def set_dnd_getter(self, getter) -> None:
        """DND 状态查询（tray 创建后晚绑定：源头停发的实时依据）。"""
        self._dnd_getter = getter or (lambda: False)

    def set_enabled(self, on: bool) -> bool:
        """开：启动采集+定时；关：全停（摄像头灯灭）。返回操作是否成功。

        ⚠️ 关闭永远返回 True（2026-09-12 修复）：旧实现 OFF 恒返 False，上游
        把「关闭成功」当「打开失败」→ 不写回 settings.yaml + 弹错误的
        「未检测到摄像头」警告 → 重启后摄像头被旧配置复活（真机「关了又开」根因）。
        """
        if on:
            if not self._capture.start():
                return False            # 无设备/权限：保持关
            self._timer.start()
            self._notify("摄像头感知已开启（画面将定期发送至所配置的 VLM）")
            return True
        self._timer.stop()
        self._capture.stop()
        self._notify("摄像头感知已关闭")
        return True

    def _tick(self) -> None:
        if not self._connected_getter():
            return                      # 断连不发
        if self._dnd_getter():
            return                      # DND 源头停发（不抓帧不上传）
        self._capture.request_frame()

    def _on_capture_failed(self, reason: str) -> None:
        """采集器异步失败（设备被占用等，start() 后经 errorOccurred 到达）：
        停采样循环 + 可见化——「开启成功」的托盘提示已出过，失败必须同样可见。"""
        if not self._timer.isActive():
            return                      # 未在运行（start() 同步失败路径已处理）
        self._timer.stop()
        self._capture.stop()
        self._notify(f"摄像头感知已中断并关闭（{reason}）；"
                     "设备可用后在设置或托盘重新打开。")

    def _on_frame(self, jpeg: bytes) -> None:
        if not self._connected_getter() or self._dnd_getter():
            return                      # 帧到达时状态已变 → 丢弃
        self._ws.send_text(CameraFrame(
            image=base64.b64encode(jpeg).decode("ascii")).to_json())
