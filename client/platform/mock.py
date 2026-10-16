"""内存态可编程 Mock 平台（仅测试注入；生产代码经 get_platform 拿 Win32）。

与 Win32 的语义契约见 test_platform_provider.py 的 run_platform_contract_suite：
  - 互斥量表是类级共享（模拟操作系统互斥量的跨实例命名空间）；
  - audio_peak 默认 -1.0（不可用）；foreground 默认 None（取不到）；
  - fail=False 的时机与含义必须与 Win32 一致。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, ClassVar

from client.platform.base import ForegroundInfo, is_valid_combo_format


class MockPlatform:
    name = "mock"

    # 类级共享：模拟 OS 命名互斥量（跨实例可见）；测试用 reset_mutexes() 复位
    _shared_mutexes: ClassVar[set[str]] = set()
    _shared_mutexes_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self.foreground: Optional[ForegroundInfo] = None   # None = 取不到
        self.locked: bool = False
        self.last_input_seconds: float = 0.0               # -1.0 = 未知
        self.audio_peak: float = -1.0                      # -1.0 = 不可用
        self.foreground_fullscreen: bool = False           # 陪玩域判据（M-PLAY）
        self.audio_active: Optional[set[str]] = set()      # None = 不可判定（M-PLAY）
        self.hotkeys: dict[str, Callable[[], None]] = {}
        self.register_ok: bool = True
        self.fail_mutex: bool = False                      # True = 模拟 CreateMutexW 本身失败
        self.dialogs: list[tuple[str, str]] = []

    @classmethod
    def reset_mutexes(cls) -> None:
        with cls._shared_mutexes_lock:
            cls._shared_mutexes.clear()

    def acquire_single_instance_lock(self, name: str) -> bool:
        with self._shared_mutexes_lock:
            if self.fail_mutex or name in self._shared_mutexes:
                return False
            self._shared_mutexes.add(name)
            return True

    def get_foreground_window(self) -> Optional[ForegroundInfo]:
        return self.foreground

    def is_session_locked(self) -> bool:
        return self.locked

    def get_last_input_seconds(self) -> float:
        return self.last_input_seconds

    def is_foreground_fullscreen(self) -> bool:
        return self.foreground_fullscreen

    def get_audio_active_processes(self) -> Optional[set[str]]:
        return None if self.audio_active is None else set(self.audio_active)

    def get_system_audio_peak(self) -> float:
        return self.audio_peak

    def register_hotkey(self, combo: str, callback: Callable[[], None]) -> bool:
        if (not self.register_ok or combo in self.hotkeys
                or not is_valid_combo_format(combo)):
            return False  # 被占用/注册失败/非法组合：与 Win32 语义一致
        self.hotkeys[combo] = callback
        return True

    def show_error_dialog(self, title: str, text: str) -> None:
        self.dialogs.append((title, text))

    def fire_hotkey(self, combo: str) -> None:
        """测试辅助：模拟热键触发。"""
        callback = self.hotkeys.get(combo)
        if callback is not None:
            callback()
