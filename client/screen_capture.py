"""识屏采集（M+3.1）—— 嗅探状态机 + 全帧捕获。

状态机（plan 定版迁移）：
  ACTIVE ──输入空闲≥3min（get_last_input_seconds()）──→ IDLE_WATCHING
  IDLE_WATCHING：60s 一次 160px 灰度均值 hash（**纯本地零上传**，只比不发）；
    hash 显著变化置 dirty；🔑 锁屏（is_session_locked）清 dirty——锁屏期间的
    画面变化不算数，也不采样（锁屏内容无意义）；回座瞬间 dirty 才截全帧上行
    （source="idle"）。手动热键（默认 Ctrl+Shift+M）任意时刻截全帧
    （source="hotkey"）。

隐私三道闸之客户端半边：默认 behavior.idle_sniff.enabled: false；OCR 未装
extras → 闲置嗅探自动禁用（§4.1「OCR 缺失/崩溃」行），热键仍可手动触发
（无 ocr 字段 → 服务端走 VLM 路）。

结构：状态机本体纯逻辑零 Qt（platform/捕获/hash/时钟全注入，状态机迁移
单测 mock platform——plan 验收）；Qt 落图只在模块级薄函数（grab/编码/hash）。
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Callable, Optional

from server.protocol import ScreenOcrLine, ScreenShot

logger = logging.getLogger(__name__)

IDLE_THRESHOLD_S = 180.0     # 输入空闲 ≥3min → IDLE_WATCHING（plan 定版；
                             #   仅作缺省——run_client 现按 idle_sniff.hours 传参覆盖）
SAMPLE_INTERVAL_S = 60.0     # IDLE_WATCHING 的 hash 采样间隔（plan 定版）
HASH_SIZE = 160              # 160px 灰度均值 hash（plan 定版；25600 位 aHash）
_SIGNIFICANT_RATIO = 0.125   # 「显著变化」= 位距 ≥ 总位数 12.5%（比例制：
                             # 160×160 实 hash 与测试短向量同一判据）
DEFAULT_HOTKEY = "Ctrl+Shift+M"
_DND_LOG_GAP_S = 300.0       # DND 暂停期节流日志间隔（2026-09-17 事故补）


def idle_threshold_from_cfg(idle_cfg: dict | None) -> float:
    """idle_sniff.hours（小时）→ 进入嗅探的空闲秒阈值。

    2026-09-17 修正半接线：此前 run_client 未把该键传给 sniffer，客户端恒用
    硬编码 180s——「无输入 0.5 小时进入嗅探」实际 3 分钟就进（日志实证
    503s/182s）。缺省/非法 fail-open 回落 4h（example 缺省）；地板 60s 防
    误配 0 造成每 tick 都采样。
    """
    default_hours = 4.0
    try:
        hours = float((idle_cfg or {}).get("hours", default_hours))
    except (TypeError, ValueError):
        hours = default_hours
    return max(60.0, hours * 3600.0)


def mean_hash(image) -> str:
    """QImage → 160×160 灰度均值 hash 的 hex（aHash：像素>均值=1）。

    全本地计算；仅在全帧上行时随帧携带（服务端 ① 去重闸比对用）。
    """
    import numpy as np
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    img = image.convertToFormat(
        QImage.Format.Format_Grayscale8).scaled(
        HASH_SIZE, HASH_SIZE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation)
    stride = img.bytesPerLine()
    arr = np.frombuffer(bytes(img.constBits()), dtype=np.uint8).reshape(
        img.height(), stride)[:, :HASH_SIZE]
    return np.packbits(arr > arr.mean()).tobytes().hex()


def hash_distance(hex_a: str, hex_b: str) -> int:
    """两个 hash hex 的 Hamming 距离（异常输入按最大距离=完全不同处理）。"""
    try:
        return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()
    except (ValueError, TypeError):
        return -1


def hash_changed(hex_a: Optional[str], hex_b: Optional[str]) -> bool:
    """「显著变化」判定：位距 ≥ 12.5% 总位数；缺失/非法按变化处理。"""
    if not hex_a or not hex_b:
        return True
    dist = hash_distance(hex_a, hex_b)
    if dist < 0:
        return True
    return dist >= max(1, int(min(len(hex_a), len(hex_b)) * 4 * _SIGNIFICANT_RATIO))


def grab_screen_image():
    """全屏抓取为 QImage；无屏/抓取失败返回 None（绝不抛异常）。"""
    try:
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen is None:
            return None
        img = screen.grabWindow(0).toImage()
        return None if img.isNull() else img
    except Exception:
        logger.debug("屏幕抓取失败", exc_info=True)
        return None


def frame_jpeg_b64(image) -> str:
    """QImage → base64 JPEG（短边 ≤1024px 契约：长边钳 1024 严格更紧）。"""
    from client.camera_capture import qimage_to_jpeg_bytes

    return base64.b64encode(
        qimage_to_jpeg_bytes(image, max_side=1024)).decode("ascii")


class ScreenSniffer:
    """嗅探状态机（纯逻辑）。state ∈ {"active", "idle_watching"}。

    send 收 ScreenShot 线格式；capture() → QImage（None=失败）；
    hash_fn(image) → hex；ocr_run(image, hash_hex) → [(text, conf)] | None
    （None=OCR 不可用/失败——闲置嗅探自动禁用的判据，§4.1）。
    """

    def __init__(self, *, send: Callable[[str], None],
                 platform=None,
                 ocr_run: Optional[Callable] = None,
                 ocr_ok: Optional[Callable[[], bool]] = None,
                 enabled: bool = False,
                 capture: Optional[Callable] = None,
                 hash_fn: Optional[Callable[[object], str]] = None,
                 jpeg_b64_fn: Optional[Callable[[object], str]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 connected_getter: Optional[Callable[[], bool]] = None,
                 dnd_getter: Optional[Callable[[], bool]] = None,
                 idle_threshold_s: float = IDLE_THRESHOLD_S,
                 sample_interval_s: float = SAMPLE_INTERVAL_S,
                 companion_cfg: Optional[dict] = None,
                 notify: Optional[Callable[[str], None]] = None) -> None:
        self._send = send
        self._platform = platform
        self._ocr_run = ocr_run
        self._ocr_ok = ocr_ok or (lambda: self._ocr_run is not None)
        self._enabled = bool(enabled)
        self._capture = capture or grab_screen_image
        self._hash_fn = hash_fn or mean_hash
        self._jpeg_b64_fn = jpeg_b64_fn or frame_jpeg_b64
        self._clock = clock
        self._connected_getter = connected_getter or (lambda: True)
        self._dnd_getter = dnd_getter or (lambda: False)
        self._notify = notify or (lambda msg: logger.info("%s", msg))
        self.idle_threshold_s = float(idle_threshold_s)
        self.sample_interval_s = float(sample_interval_s)
        self.state = "active"
        self.dirty = False
        self._last_hash: Optional[str] = None
        self._last_sample = -1e18
        self._dnd_last_log = -1e18           # DND 暂停期节流日志（_DND_LOG_GAP_S）
        self._dnd_companion_hinted = False   # 勿扰杀陪玩的气泡提示旗（episode 单次）
        self._init_companion(companion_cfg or {})

    def _init_companion(self, comp: dict) -> None:
        """陪玩域（M-PLAY）：名单/节流/会话参数解析（fail-open 全缺省）。"""
        self._companion_apps: dict[str, str] = {}      # 进程名(小写) → game|video|web
        invalid = 0
        for item in (comp.get("apps") or []):
            # 全角标点归一（2026-09-12 真机陷阱：中文 IME 的「，」不被半角逗号
            # 分隔，整串并成单条目 → kind 含余串 → 整条非法静默跳过）；UI 侧
            # _wl_encode 同步归一。条目内再按半角逗号切分以容错合并条目。
            text = str(item).replace("，", ",").replace("：", ":")
            for part in text.split(","):
                part = part.strip()
                if not part:
                    continue
                name, _, kind = part.partition(":")
                name = name.strip().lower()
                kind = kind.strip().lower()
                # 缺类型不默认 game（2026-09-12）：浏览器缺类型会静默丢失防剧透语义
                # ——按非法处理 + 托盘提示，逼出正确格式
                if name and kind in ("game", "video", "web"):
                    self._companion_apps[name] = kind
                else:
                    invalid += 1
                    logger.warning("陪玩名单条目非法已跳过：%r（格式 进程名:类型）", part)
        self._companion_enabled = bool(comp.get("enabled")) and bool(self._companion_apps)
        if invalid and comp.get("enabled"):
            # 🔑 可见性加固（2026-09-12 真机教训：格式写错=静默失效，用户无感）
            hint = (f"陪玩名单有 {invalid} 条格式非法已忽略（应为 进程名:类型，"
                    "类型 game/video/web）")
            if not self._companion_apps:
                hint += "；无有效条目，陪玩模式不会工作"
            self._notify(hint)
        self._companion_interval_s = max(2.0, float(comp.get("sample_interval_s", 5)))
        self._scene_stable_s = max(0.0, float(comp.get("scene_stable_s", 2.0)))
        self._companion_cooldown_s = max(0.0, float(comp.get("cooldown_min", 3)) * 60.0)
        self._max_per_session = max(1, int(comp.get("max_per_session", 12)))
        self._session_gap_s = max(60.0, float(comp.get("session_gap_min", 10)) * 60.0)
        self._video_silence_s = max(1.0, float(comp.get("video_silence_s", 8)))
        self._companion_last_sample = -1e18
        self._companion_last_hash: Optional[str] = None
        self._scene_candidate_at = -1e18
        self._companion_last_fire = -1e18
        self._companion_count = 0
        self._companion_last_seen = -1e18
        self._session_cap_notified = False
        self._audio_last_active = -1e18
        self._video_silence_fired = False

    @property
    def sniffing(self) -> bool:
        """闲置嗅探是否在跑：开关开 **且** OCR 真实可用（§4.1 自动禁用）。"""
        try:
            return self._enabled and bool(self._ocr_ok())
        except Exception:
            return False

    def tick(self) -> None:
        """周期驱动（QTimer ~5s；各域内部再按各自间隔采样）。

        域分派（M-PLAY）：陪玩域 > 闲置嗅探——名单内应用全屏时陪玩接管本 tick，
        闲置嗅探挂起（闲置域与陪玩域互斥，画面出门语义不同）。
        """
        platform = self._platform
        if platform is None:
            return
        if self._dnd_getter():
            self._tick_dnd_paused(platform)
            return                                   # DND 源头停（识屏两域同规）
        self._dnd_companion_hinted = False           # 勿扰关闭即复位（再开时重提一次）
        if platform.is_session_locked():
            if self.dirty:
                logger.info("锁屏：清 dirty（锁屏期间画面变化不计）")
            self.dirty = False
            return                                   # 锁屏不采样
        if self._companion_enabled and self._companion_tick(platform):
            return                                   # 陪玩域接管（每次 tick 只评估一次）
        if not self.sniffing:
            return                                   # 默认关 / OCR 缺失 → 闲置域不转
        idle = platform.get_last_input_seconds()
        if idle < 0:
            return                                   # 取不到输入时长：按未知不迁移
        if self.state == "active":
            if idle >= self.idle_threshold_s:
                baseline = self._sample_hash()
                if baseline is None:
                    return                           # 抓屏失败：留在 ACTIVE 下轮重试
                self._last_hash = baseline
                self._last_sample = self._clock()
                self.state = "idle_watching"
                self.dirty = False
                logger.info("进入闲置嗅探（空闲 %.0fs，基线 hash 已采）", idle)
            return
        # ---- IDLE_WATCHING ----
        if idle < self.idle_threshold_s:
            self._return_to_seat()
            return
        now = self._clock()
        if now - self._last_sample < self.sample_interval_s:
            return
        self._last_sample = now
        current = self._sample_hash()
        if current is None:
            return
        if hash_changed(current, self._last_hash):
            if not self.dirty:
                logger.info("闲置期画面显著变化 → dirty")
            self.dirty = True
        self._last_hash = current

    def _tick_dnd_paused(self, platform) -> None:
        """DND 暂停期的运行时可见性（2026-09-17 事故补：此前静默 return，
        陪玩/闲置嗅探「无声死」，用户零感知——与 ASR mock 同类静默失效）。

        ①节流 INFO 进 client.log（防 5s 一条的日志刷屏）；②名单内应用正处
        全屏 → 气泡提示一次「陪玩吐槽已暂停」（episode 单次：离开全屏或
        关闭勿扰后复位，再进入重新提示）。
        """
        now = self._clock()
        if now - self._dnd_last_log >= _DND_LOG_GAP_S:
            self._dnd_last_log = now
            logger.info("勿扰模式（DND）开启中：陪玩吐槽/闲置嗅探暂停")
        if not self._companion_enabled:
            return
        info = platform.get_foreground_window()
        name = (info.process.lower() if info else "")
        if name in self._companion_apps and platform.is_foreground_fullscreen():
            if not self._dnd_companion_hinted:
                self._dnd_companion_hinted = True
                self._notify("勿扰模式开启中，陪玩吐槽已暂停；关闭勿扰后恢复")
        else:
            self._dnd_companion_hinted = False

    def handle_hotkey(self) -> None:
        """手动热键：任意状态直接截全帧（不依赖嗅探开关/OCR 可用）。"""
        self._send_frame("hotkey")

    # ---------------- 陪玩域（M-PLAY） ----------------
    def _companion_tick(self, platform) -> bool:
        """陪玩域判定与触发。返回 True=本 tick 被陪玩域接管（闲置嗅探挂起）。"""
        now = self._clock()
        info = platform.get_foreground_window()
        name = (info.process.lower() if info else "")
        kind = self._companion_apps.get(name)
        if kind is None or not platform.is_foreground_fullscreen():
            if (self._companion_last_seen > 0
                    and now - self._companion_last_seen >= self._session_gap_s):
                logger.info("陪玩会话结束（本次搭话 %d 次）", self._companion_count)
                self._companion_count = 0
                self._companion_last_seen = -1e18
                self._companion_last_hash = None
                self._scene_candidate_at = -1e18
                self._session_cap_notified = False
                self._audio_last_active = -1e18
                self._video_silence_fired = False
            return False
        if self._companion_last_seen < 0:
            logger.info("陪玩模式接管（%s:%s）", name, kind)
            self._notify(f"陪玩模式接管：{name}（{kind}）——场景切换/静默窗口时吐槽")
        self._companion_last_seen = now
        audio = platform.get_audio_active_processes()
        # 音频不可判定（None）→ 保守按有声：web 条目只走静默窗口（防剧透）
        audio_active = True if audio is None else name in audio
        effective = kind
        if kind == "web":
            effective = "video" if audio_active else "game"
        if effective == "video":
            self._companion_video_tick(name, now, audio_active)
        else:
            self._companion_scene_tick(now)
        return True

    def _companion_video_tick(self, name: str, now: float,
                              audio_active: bool) -> None:
        """video/web有声 语义：音频静默 ≥ video_silence_s 的窗口触发一次
        （暂停/片尾），音频恢复自动重新武装——正片播放期间永不触发（防剧透）。"""
        if audio_active:
            self._audio_last_active = now
            self._video_silence_fired = False
            return
        if self._audio_last_active < 0:              # 会话起步即无声：从现在起算
            self._audio_last_active = now
            return
        if self._video_silence_fired:
            return
        if now - self._audio_last_active < self._video_silence_s:
            return
        self._video_silence_fired = True             # 同一段静默只发一次
        if self._companion_throttled(now):
            return
        self._fire_companion(now)

    def _companion_scene_tick(self, now: float) -> None:
        """game/web无声 语义：hash 大变化后稳定 ≥ scene_stable_s 才触发
        （滤转场/镜头闪切等瞬时噪声）；节流与上限在触发出口统一把守。"""
        if now - self._companion_last_sample < self._companion_interval_s:
            return
        self._companion_last_sample = now
        current = self._sample_hash()
        if current is None:
            return
        prev = self._companion_last_hash
        self._companion_last_hash = current
        if prev is not None and hash_changed(current, prev):
            self._scene_candidate_at = now           # 场景切换：待稳定确认
            return
        if (self._scene_candidate_at > 0
                and now - self._scene_candidate_at >= self._scene_stable_s):
            self._scene_candidate_at = -1e18         # 已确认稳定，无论是否发都销案
            if not self._companion_throttled(now):
                self._fire_companion(now)

    def _companion_throttled(self, now: float) -> bool:
        if now - self._companion_last_fire < self._companion_cooldown_s:
            return True
        if self._companion_count >= self._max_per_session:
            if not self._session_cap_notified:
                self._session_cap_notified = True
                self._notify(f"陪玩模式：本次会话搭话已达上限（{self._max_per_session} 次），"
                             "休息一下～")
            return True
        return False

    def _fire_companion(self, now: float) -> None:
        self._companion_last_fire = now
        self._companion_count += 1
        self._send_frame("companion")
        logger.info("陪玩场景帧已上行（本会话第 %d 次）", self._companion_count)

    # ---------------- 内部 ----------------
    def _return_to_seat(self) -> None:
        """回座：dirty 才截全帧（source="idle"），随后回 ACTIVE。"""
        was_dirty = self.dirty
        self.dirty = False
        self.state = "active"
        if was_dirty:
            self._send_frame("idle")

    def _sample_hash(self) -> Optional[str]:
        img = self._capture()
        if img is None:
            return None
        return self._hash_fn(img)

    def _send_frame(self, source: str) -> None:
        if not self._connected_getter():
            return                                   # 断连不发（与摄像头同规）
        img = self._capture()
        if img is None:
            return
        hash_hex = self._hash_fn(img)
        lines = None
        if self._ocr_run is not None:
            try:
                result = self._ocr_run(img, hash_hex)
            except Exception:
                logger.warning("本地 OCR 异常，本帧按无 OCR 上行（VLM 路）",
                               exc_info=True)
                result = None
            if result:
                lines = [ScreenOcrLine(text=str(t), conf=float(c))
                         for t, c in result]
        self._send(ScreenShot(image=self._jpeg_b64_fn(img), source=source,
                              hash=hash_hex, ocr=lines).to_json())
        logger.info("识屏全帧已上行（source=%s，ocr=%s）",
                    source, "本地文本" if lines else "无（VLM 路）")


class _HotkeyMarshaller:
    """热键回调投递到 GUI 线程执行。

    win32 热键在独立消息泵线程触发回调，而回调链里的截屏
    （QScreen.grabWindow 建 QPixmap）是 Qt 明令禁止的非 GUI 线程操作——
    此前泵线程直调，与 GUI 线程 30ms 一次的抓屏竞态（随机崩溃/静默失败）。
    经 GUI 线程 QObject 的 queued 信号转发；模块级列表保活防 GC。
    """

    _keepalive: list[object] = []

    def __init__(self, callback: Callable[[], None]) -> None:
        from PySide6.QtCore import QObject, Qt, Signal

        self._callback = callback

        class _Relay(QObject):
            fired = Signal()

        self._relay = _Relay()
        self._relay.fired.connect(self._run, Qt.ConnectionType.QueuedConnection)
        _HotkeyMarshaller._keepalive.append(self)

    def __call__(self) -> None:
        self._relay.fired.emit()   # 任意线程可发射；落地在 relay 所属（GUI）线程

    def _run(self) -> None:
        try:
            self._callback()
        except Exception:
            logger.exception("识屏热键回调异常")


def register_screen_hotkey(platform, combo: str, callback: Callable[[], None],
                           notify: Callable[[str], None]) -> bool:
    """注册识屏热键；失败提示（§4.1：识屏退化为设置窗按钮触发——M+3.5 接）。

    callback 经 _HotkeyMarshaller 投递 GUI 线程（泵线程直调会在非 GUI 线程
    建 QPixmap，见该类 docstring）。
    notify 链异常不反噬装配（真机实例占用热键属常态，冒烟实测暴露）。
    """
    marshaller = _HotkeyMarshaller(callback)
    if platform.register_hotkey(combo, marshaller):
        return True
    try:
        notify(f"识屏热键 {combo} 注册失败（被占用或组合非法）。"
               "手动截屏暂不可用，可在设置中改键后重启。")
    except Exception:
        logger.warning("热键注册失败提示展示异常（忽略）", exc_info=True)
    return False
