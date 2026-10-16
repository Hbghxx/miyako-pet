"""平台抽象层接口（原则 12：系统调用隔离）。

这是「测试接缝」而非跨平台承诺（§4.2）：业务代码只经 PlatformProvider
访问系统能力；ctypes/pycaw 全部集中在 platform/win32.py
（import 约束 A，test_platform_provider.py 用 AST 扫描执法）。

契约总纲：全部方法「失败返回安全值，绝不抛异常」——
  互斥量获取失败   → False（是否退出由调用方决定，win32 不替调用方退出）
  前台窗口取不到   → None
  会话锁定查询失败 → False（按未锁定处理）
  输入空闲取不到   → -1.0（调用方按未知处理）
  音频峰值不可用   → -1.0（§4.1「音频峰值监测不可用」行的保守语义）
  热键注册失败     → False
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Protocol


@dataclass(frozen=True)
class ForegroundInfo:
    """前台窗口信息（State 消息上行载荷的数据源，M+2.2 消费）。"""

    title: str
    process: str


_VALID_MODIFIERS = frozenset({"ctrl", "control", "alt", "menu", "shift", "win", "windows"})
_FKEY_RE = re.compile(r"^f([1-9]|1[0-9]|2[0-4])$")


def is_valid_combo_format(combo: str) -> bool:
    """热键组合文法（契约级，Mock/Win32 共用）：mod+…+key。

    mod ∈ {ctrl, alt, shift, win}（含别名）；key 为单个字母/数字或 F1~F24。
    非法组合在所有实现上都必须返回注册失败（语义一致的一部分）。
    """
    parts = [p.strip().lower() for p in (combo or "").split("+") if p.strip()]
    if not parts:
        return False
    for part in parts[:-1]:
        if part not in _VALID_MODIFIERS:
            return False
    key = parts[-1]
    return (len(key) == 1 and key.isalnum()) or bool(_FKEY_RE.match(key))


class PlatformProvider(Protocol):
    """平台能力七方法契约。Mock 与 Win32 必须语义一致（不只是签名一致）。"""

    def acquire_single_instance_lock(self, name: str) -> bool:
        """获取命名互斥量：成功 True；已被占用或创建失败 False。"""

    def get_foreground_window(self) -> Optional[ForegroundInfo]:
        """前台窗口；取不到（无前台/进程名不可得/失败）返回 None。"""

    def is_session_locked(self) -> bool:
        """会话是否锁定；查询失败按未锁定（False）处理。"""

    def get_last_input_seconds(self) -> float:
        """距最后一次用户输入的秒数；取不到返回 -1.0。"""

    def is_foreground_fullscreen(self) -> bool:
        """前台窗口覆盖所在显示器（真全屏，或最大化=覆盖工作区；M-PLAY，
        2026-09-12 放宽）；查询失败/无前台 → False，不抛异常。"""

    def get_audio_active_processes(self) -> Optional[set[str]]:
        """正在出声的进程名集合（小写；陪玩 web/video 语义判别，M-PLAY）。

        None = 不可判定（pycaw 缺失/无设备）——调用方按保守语义处理
        （web 条目视为有声 → 只走静默窗口，防剧透）；空集 = 正常无人出声。
        """

    def get_system_audio_peak(self) -> float:
        """系统混音峰值，0.0~1.0；不可用返回 -1.0，不抛异常。"""

    def register_hotkey(self, combo: str, callback: Callable[[], None]) -> bool:
        """注册全局热键（如 "Ctrl+Shift+M"）；失败/被占用/组合非法返回 False。"""

    def show_error_dialog(self, title: str, text: str) -> None:
        """模态错误对话框；显示失败仅记日志，不抛异常。"""


_provider: Optional[PlatformProvider] = None


def get_platform() -> PlatformProvider:
    """平台工厂：运行时返回 Win32 实现，测试经 set_platform 注入 Mock。

    刻意不做 settings 级平台选择（§4.3 否决清单：无此需求，多一个开关
    就多一个必须测试的组合）。Win32 实现惰性导入，避免平台层以外的
    代码在 import 链上碰到 ctypes。
    """
    global _provider
    if _provider is None:
        from client.platform.win32 import Win32Platform

        _provider = Win32Platform()
    return _provider


def set_platform(provider: Optional[PlatformProvider]) -> None:
    """测试注入点：传 None 恢复运行时默认（Win32）。仅测试代码调用。"""
    global _provider
    _provider = provider
