"""平台状态上报（M+2.2）—— 变更触发 + 最小间隔 5s 节流。

每 poll_ms 采集一次 platform 前台窗口/锁屏 + 托盘 DND，键变化且距上次
发送 ≥5s 才发 state{foreground, locked, dnd}（服务端仅存内存态）。
首次 tick 必发（服务端重启后需要拿到初始状态）。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer

from server.behavior import should_send_state
from server.protocol import ForegroundInfo, State


class StateReporter(QObject):
    """周期采集 → 节流上行。send 注入 ws.send_text（引擎断线期自动缓冲）。"""

    def __init__(self, platform, send: Callable[[str], None],
                 get_dnd: Callable[[], bool], parent=None, *,
                 poll_ms: int = 1000, min_gap_s: float = 5.0,
                 clock: Optional[Callable[[], float]] = None) -> None:
        super().__init__(parent)
        self._platform = platform
        self._send = send
        self._get_dnd = get_dnd
        self._min_gap_s = min_gap_s
        self._clock = clock or time.monotonic
        self._last_key: Optional[tuple] = None
        self._last_sent = float("-inf")
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self.tick)

    def start(self) -> None:
        self.tick()          # 首次立即上报（初始状态）
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def tick(self) -> None:
        try:
            info = self._platform.get_foreground_window()
            fg = {"title": info.title, "process": info.process} if info else None
            locked = bool(self._platform.is_session_locked())
            dnd = bool(self._get_dnd())
        except Exception:
            fg, locked, dnd = None, None, False   # 采集失败按 null 上报（gate fail-closed）
        key = (fg, locked, dnd)
        now = self._clock()
        if not should_send_state(self._last_key, key, now - self._last_sent,
                                 self._min_gap_s):
            return
        self._last_key = key
        self._last_sent = now
        state = State(foreground=ForegroundInfo(**fg) if fg else None,
                      locked=locked, dnd=dnd)
        self._send(state.to_json())
