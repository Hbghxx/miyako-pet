"""Windows 平台实现 —— 全项目唯一的 ctypes/pycaw 驻留地（import 约束 A）。

降级语义落在代码里（不是注释里）：所有系统调用包在 try/except 内，
失败返回安全值（False / None / -1.0），绝不抛异常——
  互斥量已被占用或创建失败 → False（退出决策在 pet_window，不在本层）
  前台窗口/进程名取不到     → None
  锁屏查询失败             → False（按未锁定处理）
  输入空闲取不到           → -1.0
  pycaw 缺失（extras aec 未装）/无默认输出设备 → 音频峰值 -1.0（§4.1 两行保守语义）
  热键注册失败/组合非法     → False
  对话框显示失败           → 仅日志
"""
from __future__ import annotations

import logging
import ntpath
import threading
from typing import Callable, Optional

from client.platform.base import ForegroundInfo, is_valid_combo_format

logger = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_READOBJECTS = 0x0001
MB_ICONERROR = 0x00000010
WM_HOTKEY = 0x0312
VK_F1 = 0x70

_MOD_MAP = {
    "ctrl": 0x0002, "control": 0x0002,
    "alt": 0x0001, "menu": 0x0001,
    "shift": 0x0004,
    "win": 0x0008, "windows": 0x0008,
}

MONITOR_DEFAULTTONEAREST = 2
_FULLSCREEN_TOLERANCE_PX = 4     # 窗口矩形≈显示器矩形判全屏的容差（无边框窗取整误差）
_AUDIO_ACTIVE_THRESHOLD = 1e-3   # 会话峰值高于此视为「正在出声」（M-PLAY）

import ctypes as _ctypes  # noqa: E402  （import 约束 A：本文件是唯一 ctypes 驻留地）
from ctypes import wintypes as _wtypes  # noqa: E402


class _RECT(_ctypes.Structure):
    _fields_ = [("left", _wtypes.LONG), ("top", _wtypes.LONG),
                ("right", _wtypes.LONG), ("bottom", _wtypes.LONG)]


class _MONITORINFO(_ctypes.Structure):
    _fields_ = [("cbSize", _wtypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", _wtypes.DWORD)]


def _covers_rect(rect, area, tol: int) -> bool:
    """rect 是否覆盖 area（四边均在容差内）。纯函数（单测直测）。"""
    return (rect.left <= area.left + tol and rect.top <= area.top + tol
            and rect.right >= area.right - tol
            and rect.bottom >= area.bottom - tol)


class Win32Platform:
    name = "win32"

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._mutex_handles: dict[str, int] = {}
        self._audio_meter = None  # pycaw 接口缓存；None=不可用（extras 缺失或设备缺失）
        self._audio_meter_checked = False
        self._bind_signatures()

    def _bind_signatures(self) -> None:
        """显式签名：64 位下 HWND/HANDLE 是 64 位指针，默认 restype c_int 会截断。"""
        ct, wt, user32, kernel32 = self._ct, self._wt, self._user32, self._kernel32
        kernel32.CreateMutexW.restype = wt.HANDLE
        kernel32.CreateMutexW.argtypes = [wt.HANDLE, wt.BOOL, wt.LPCWSTR]
        kernel32.CloseHandle.restype = wt.BOOL
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wt.HANDLE, wt.DWORD, wt.LPWSTR, ct.POINTER(wt.DWORD)]
        kernel32.GetTickCount.restype = wt.DWORD
        user32.GetForegroundWindow.restype = wt.HWND
        user32.GetWindowTextLengthW.restype = ct.c_int
        user32.GetWindowTextLengthW.argtypes = [wt.HWND]
        user32.GetWindowTextW.restype = ct.c_int
        user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ct.c_int]
        user32.GetWindowThreadProcessId.restype = wt.DWORD
        user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ct.POINTER(wt.DWORD)]
        user32.GetLastInputInfo.restype = wt.BOOL
        user32.OpenInputDesktop.restype = wt.HANDLE
        user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        user32.CloseDesktop.restype = wt.BOOL
        user32.CloseDesktop.argtypes = [wt.HANDLE]
        user32.RegisterHotKey.restype = wt.BOOL
        user32.RegisterHotKey.argtypes = [wt.HWND, ct.c_int, wt.UINT, wt.UINT]
        user32.UnregisterHotKey.restype = wt.BOOL
        user32.UnregisterHotKey.argtypes = [wt.HWND, ct.c_int]
        user32.VkKeyScanW.restype = ct.c_short
        user32.VkKeyScanW.argtypes = [wt.WCHAR]
        user32.MessageBoxW.restype = ct.c_int
        user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
        user32.GetWindowRect.restype = wt.BOOL
        user32.GetWindowRect.argtypes = [wt.HWND, ct.POINTER(_RECT)]
        user32.MonitorFromWindow.restype = wt.HANDLE
        user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        user32.GetMonitorInfoW.restype = wt.BOOL
        user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ct.POINTER(_MONITORINFO)]

    # ---- [1/7] 单实例命名互斥量 ----
    def acquire_single_instance_lock(self, name: str) -> bool:
        try:
            ctypes = self._ct
            handle = self._kernel32.CreateMutexW(None, False, name)
            if not handle:
                logger.warning("CreateMutexW 创建失败（winerror=%s）",
                               ctypes.get_last_error())
                return False
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                # 已有实例持有锁：释放本次句柄，返回 False（不退出——调用方决定）
                self._kernel32.CloseHandle(handle)
                logger.warning("互斥量 %r 已存在：已有 miyako 实例在运行", name)
                return False
            self._mutex_handles[name] = int(handle)  # 持有至进程退出（不主动释放）
            return True
        except Exception:
            logger.warning("单实例锁获取异常", exc_info=True)
            return False

    # ---- [2/7] 前台窗口 ----
    def get_foreground_window(self) -> Optional[ForegroundInfo]:
        try:
            hwnd = self._user32.GetForegroundWindow()
            if not hwnd:
                return None
            title = self._window_title(hwnd)
            process = self._process_name(hwnd)
            if not process:
                # 进程名取不到（权限/瞬时失败）→ 契约规定返回 None
                return None
            return ForegroundInfo(title=title or "", process=process)
        except Exception:
            logger.warning("前台窗口查询异常", exc_info=True)
            return None

    def _window_title(self, hwnd: int) -> str:
        user32 = self._user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = self._ct.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _process_name(self, hwnd: int) -> str:
        user32, kernel32 = self._user32, self._kernel32
        pid = self._wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, self._ct.byref(pid))
        if not pid.value:
            return ""
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            logger.debug("OpenProcess 失败 pid=%s", pid.value)
            return ""
        try:
            size = self._wt.DWORD(512)
            buf = self._ct.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(process, 0, buf, self._ct.byref(size)):
                return ""
            return ntpath.basename(buf.value)
        finally:
            kernel32.CloseHandle(process)

    # ---- [3/7] 会话锁定 ----
    def is_session_locked(self) -> bool:
        try:
            # 输入桌面在锁屏时打不开 → 打不开即视为锁定
            handle = self._user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
            if handle:
                self._user32.CloseDesktop(handle)
                return False
            return True
        except Exception:
            logger.warning("锁屏查询异常，按未锁定处理", exc_info=True)
            return False

    # ---- [4/7] 输入空闲 ----
    def get_last_input_seconds(self) -> float:
        try:
            ctypes, wintypes = self._ct, self._wt

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not self._user32.GetLastInputInfo(ctypes.byref(lii)):
                return -1.0
            now_tick = self._kernel32.GetTickCount()
            elapsed_ms = (now_tick - lii.dwTime) & 0xFFFFFFFF  # 49.7 天回绕保护
            return elapsed_ms / 1000.0
        except Exception:
            logger.warning("输入空闲查询异常", exc_info=True)
            return -1.0

    # ---- [5/7] 系统音频峰值（pycaw，extras aec）----
    def get_system_audio_peak(self) -> float:
        try:
            if not self._audio_meter_checked:
                self._audio_meter = self._init_audio_meter()
                self._audio_meter_checked = True
            if self._audio_meter is None:
                return -1.0
            peak = float(self._audio_meter.GetPeakValue())
            if peak != peak:  # NaN 防御
                return -1.0
            return max(0.0, min(1.0, peak))
        except Exception:
            logger.warning("系统音频峰值查询异常", exc_info=True)
            return -1.0

    def _init_audio_meter(self):
        """pycaw 初始化；任何失败（extras 缺失/无默认设备）→ None → 上层返回 -1。"""
        try:
            import comtypes  # pycaw 依赖
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

            device = AudioUtilities.GetSpeakers()
            if device is None:
                logger.info("无默认音频输出设备，系统音频峰值不可用")
                return None
            # 新版 pycaw 的 GetSpeakers 返回 AudioDevice 包装（真 IMMDevice 在 ._dev）；
            # 旧版直接返回 IMMDevice——两种形态都兼容。
            imm_device = getattr(device, "_dev", device)
            interface = imm_device.Activate(
                IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None)
            return interface.QueryInterface(IAudioMeterInformation)
        except ImportError:
            logger.info("pycaw 未安装（extras aec），系统音频峰值返回 -1")
            return None
        except Exception:
            logger.info("音频峰值接口初始化失败，系统音频峰值返回 -1", exc_info=True)
            return None

    # ---- [M-PLAY] 陪玩域判据：全屏检测 + 音频会话枚举 ----
    def is_foreground_fullscreen(self) -> bool:
        """前台窗口覆盖所在显示器（真全屏，或最大化=覆盖工作区）。

        2026-09-12 用户定案放宽：最大化也计入陪玩域（浏览器最大化看视频的
        主流姿势不再需要 F11）；小角落窗口仍不算——帧抓的是整个主屏，
        覆盖判据在替「授权范围」兜底。
        """
        try:
            user32 = self._user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            rect = _RECT()
            if not user32.GetWindowRect(hwnd, self._ct.byref(rect)):
                return False
            info = _MONITORINFO()
            info.cbSize = self._ct.sizeof(_MONITORINFO)
            monitor = user32.MonitorFromWindow(
                hwnd, MONITOR_DEFAULTTONEAREST)
            if not monitor or not user32.GetMonitorInfoW(
                    monitor, self._ct.byref(info)):
                return False
            tol = _FULLSCREEN_TOLERANCE_PX
            return (_covers_rect(rect, info.rcMonitor, tol)
                    or _covers_rect(rect, info.rcWork, tol))
        except Exception:
            logger.debug("全屏检测异常（按非全屏处理）", exc_info=True)
            return False

    def get_audio_active_processes(self) -> Optional[set[str]]:
        """正在出声的进程名集合（小写）；pycaw 缺失/无设备 → None（不可判定，
        调用方按保守语义处理——陪玩 web 条目视为有声，防剧透）。空集=无人出声。"""
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

            out: set[str] = set()
            for session in AudioUtilities.GetAllSessions():
                try:
                    name = ""
                    if session.Process is not None:
                        name = (session.Process.name() or "").lower()
                    meter = session._ctl.QueryInterface(IAudioMeterInformation)
                    peak = float(meter.GetPeakValue())
                except Exception:
                    continue
                if name and peak == peak and peak > _AUDIO_ACTIVE_THRESHOLD:
                    out.add(name)
            return out
        except ImportError:
            logger.debug("pycaw 未安装（extras aec），音频会话不可判定")
            return None
        except Exception:
            logger.debug("音频会话枚举失败（按不可判定处理）", exc_info=True)
            return None

    # ---- [6/7] 全局热键（RegisterHotKey + 独立线程消息泵）----
    def register_hotkey(self, combo: str, callback: Callable[[], None]) -> bool:
        try:
            mods, vk = self._parse_combo(combo)
            if vk is None:
                logger.warning("热键组合 %r 无法解析", combo)
                return False
            registered = threading.Event()
            result: dict[str, bool] = {"ok": False}

            def pump() -> None:
                try:
                    if not self._user32.RegisterHotKey(None, 1, mods, vk):
                        logger.warning("热键 %s 注册失败（winerror=%s，可能被占用）",
                                       combo, self._ct.get_last_error())
                        result["ok"] = False
                        registered.set()
                        return
                    result["ok"] = True
                    registered.set()
                    msg = self._wt.MSG()
                    while True:
                        r = self._user32.GetMessageW(self._ct.byref(msg), None, 0, 0)
                        if r <= 0:
                            break
                        if msg.message == WM_HOTKEY and msg.wParam == 1:
                            try:
                                callback()
                            except Exception:
                                logger.exception("热键 %s 回调异常", combo)
                    self._user32.UnregisterHotKey(None, 1)
                except Exception:
                    logger.exception("热键消息泵异常")
                    result["ok"] = False
                    registered.set()

            thread = threading.Thread(target=pump, daemon=True, name="miyako-hotkey")
            thread.start()
            registered.wait(timeout=1.0)  # 等注册结果（注册在线程内完成）
            return result["ok"]
        except Exception:
            logger.warning("热键注册异常", exc_info=True)
            return False

    def _parse_combo(self, combo: str) -> tuple[int, Optional[int]]:
        """解析 "Ctrl+Shift+M" → (mods, vk)；文法非法 → (0, None)。文法契约见 base。"""
        if not is_valid_combo_format(combo):
            return 0, None
        parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
        mods = 0
        for part in parts[:-1]:
            mods |= _MOD_MAP[part]
        key = parts[-1]
        if len(key) == 1:
            return mods, ord(key.upper())
        return mods, VK_F1 + int(key[1:]) - 1

    # ---- [7/7] 错误对话框 ----
    def show_error_dialog(self, title: str, text: str) -> None:
        try:
            self._user32.MessageBoxW(None, text, title, MB_ICONERROR)
        except Exception:
            logger.error("错误对话框显示失败: %s | %s", title, text, exc_info=True)
