"""miyako 客户端入口（M1.10）：顶层崩溃防护 + 组件装配。

崩溃防护（plan M1.10）：未捕获异常 → traceback 写
%LOCALAPPDATA%\\miyako\\logs\\crash-*.log（滚动 3 份）+ 平台错误对话框。
"""
from __future__ import annotations

import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 冻结（PyInstaller exe）形态：ROOT=exe 所在目录（data/、config/ 与 exe 并排）；
# 源码形态=脚本目录（sys.path 注入供 server/client 包导入）
ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
sys.path.insert(0, str(ROOT))

# 协议解析必须模块级：_on_server_message 是模块级函数——曾因函数内导入导致
# 下行消息一到达就 NameError（被 Qt 槽吞掉，表现为「有输入无气泡」，M1.10 潜伏）
from server.protocol import (  # noqa: E402
    AudioBegin,
    AudioChunk,
    AudioEnd,
    AudioStart,
    Interrupt,
    ReplyEnd,
    Sentence,
    ToolEvent,
    UserText,
    from_json,
)
from server.logsetup import setup_logging  # noqa: E402

CRASH_KEEP = 3


def logs_dir() -> Path:
    """日志目录：实现统一在 server.logsetup（server/client 同策略），此处传本仓根。

    ROOT 取模块全局（测试经 monkeypatch rc.ROOT 模拟不可写回退，不读快照）。
    """
    from server.logsetup import logs_dir as _shared_logs_dir

    return _shared_logs_dir(ROOT)


def write_crash_log(tb_text: str, logs_dir_: Path | None = None) -> Path:
    """写入 crash-1.log；旧文件按 1→2→3 滚动（保留最近 3 份）。"""
    directory = logs_dir_ or logs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    oldest = directory / f"crash-{CRASH_KEEP}.log"
    if oldest.exists():
        oldest.unlink()
    for i in range(CRASH_KEEP - 1, 0, -1):
        src = directory / f"crash-{i}.log"
        if src.exists():
            src.rename(directory / f"crash-{i + 1}.log")
    target = directory / "crash-1.log"
    target.write_text(tb_text, encoding="utf-8")
    return target


def _confirm_camera_consent() -> bool:
    """首次开启摄像头感知的显式同意框（plan M-CAM 硬约束；原托盘逻辑迁移）。"""
    from PySide6.QtWidgets import QMessageBox

    reply = QMessageBox.warning(
        None, "开启摄像头感知",
        "即将开启摄像头感知：\n\n"
        "· 摄像头指示灯会亮起，画面将定期（默认约 45 秒一次）发送至你在 "
        "settings.yaml 中配置的 VLM 云端用于理解画面；\n"
        "· 帧与观察结果仅保存在内存，不写入任何数据库或文件；\n"
        "· 勿扰模式（DND）开启时自动停止采集与发送；\n"
        "· 你可以随时在设置中关闭（关闭后摄像头完全停止工作）。\n\n"
        "确定开启吗？",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)
    return reply == QMessageBox.StandardButton.Yes


def apply_camera_enabled(desired: bool, *, watcher, notify=None,
                         settings_path=None, confirm=None) -> bool:
    """摄像头感知启停（隐私组设置项，原托盘开关迁移）：同意框→启停→写回。

    首次开启必须过隐私同意框（plan M-CAM 硬约束，随开关迁移到设置窗路径；
    consent 存档后不再重复弹）。关闭永远成功；打开失败（无设备/被占用）
    返回 False 由调用方提示。confirm：同意框注入缝（测试假件）；
    settings_path：settings 文件注入缝（测试）。
    """
    from client.camera_watcher import SETTINGS_PATH as _CAM_SETTINGS
    from client.camera_watcher import write_camera_enabled
    from server.constants import load_settings

    path = settings_path or _CAM_SETTINGS
    notify = notify or (lambda msg: logger.info("%s", msg))
    if desired:
        consented = bool((load_settings(path).get("camera") or {}).get("consent"))
        if not consented:
            confirm_fn = confirm or _confirm_camera_consent
            if not confirm_fn():
                notify("摄像头感知保持关闭（未同意隐私说明）")
                return False
            from client.settings_window import _rewrite_key_at_path, _yaml_scalar

            _rewrite_key_at_path(path, ("camera", "consent"), "true")
    ok = watcher.set_enabled(desired)
    if not ok:
        notify("未检测到可用摄像头，摄像头感知保持关闭。")
        return False
    write_camera_enabled(desired, path)
    return True


def guarded_main(main_fn=None) -> int:
    """顶层防护：任何未捕获异常 → crash 日志 + 平台错误对话框，进程退出码 1。"""
    try:
        return (main_fn or run_app)()
    except SystemExit as exc:  # 单实例等主动退出不属于崩溃
        return int(exc.code or 0)
    except BaseException:
        tb = traceback.format_exc()
        try:
            path = write_crash_log(tb)
            hint = f"错误详情已记录：{path}"
        except Exception:
            logger.exception("crash 日志写入失败")
            path = None
            hint = "错误详情写入失败（磁盘/权限）"
        try:
            from client.platform.base import get_platform
            from server.persona import display_name

            get_platform().show_error_dialog(f"{display_name()} 崩溃了", hint)
        except Exception:
            logger.exception("错误对话框显示失败")
        return 1


def build_components(cfg: dict | None = None, *,
                     single_instance_check: bool = True,
                     start_ws: bool = True) -> dict:
    """装配全部客户端组件并接线（顺序即依赖顺序，离屏冒烟直接测本函数）。

    ui_theme 消费：pet_window.base_height → 立绘等比缩放；
    input_bar.{width,font_size} → 输入栏尺寸（M+3.5 设置窗前改配置文件调整）。
    """
    from PySide6.QtWidgets import QApplication

    from client.audio_playback import AudioPlayback
    from client.bubble import BubbleWidget
    from client.input_bar import InputBar
    from client.pet_window import PetWindow, ensure_single_instance
    from client.presenter import SpritePresenter
    from client.sprite_manager import SpriteManager
    from client.switcher import list_character_packages
    from client.tray import TrayController
    from client.ws_client import WsClient
    from server.constants import active_package_id
    from server.main import resolve_bind_host

    cfg = cfg or {}
    if single_instance_check:
        ensure_single_instance()  # 失败 → 气泡/对话框 + sys.exit(0)

    app = QApplication.instance() or QApplication(sys.argv)

    theme = cfg.get("ui_theme") or {}
    pet_theme = theme.get("pet_window") or {}
    bar_theme = theme.get("input_bar") or {}
    server_cfg = cfg.get("server") or {}
    host = resolve_bind_host(str(server_cfg.get("host", "127.0.0.1")))
    port = int(server_cfg.get("port", 9800))

    manager = SpriteManager()
    # 立绘外观（M-SPRITE）：服装/朝向/饰品，托盘「立绘」菜单驱动 + ui_theme 持久化
    pet_look = {"outfit": str(pet_theme.get("outfit") or "casual"),
                "pose_mode": str(pet_theme.get("pose_mode") or "auto")}
    manager.set_outfit(pet_look["outfit"])
    bubble = BubbleWidget()
    presenter = SpritePresenter(manager, on_bubble=bubble.show_sentence)
    window = PetWindow()
    window.set_sprite(_load_sprite_pixmap(manager),
                      target_height=int(pet_theme.get("base_height") or 0) or None)
    window.show()
    # 位置记忆（2026-09-11 用户需求）：立绘/气泡偏移/输入栏，存档坐标不在
    # 任何屏幕内（换显示器/改分辨率）→ 忽略回默认位
    from client.ui_layout import load_ui_positions, point_on_screen, save_ui_positions

    saved_ui = load_ui_positions()
    if saved_ui["pet"] is not None and point_on_screen(*saved_ui["pet"]):
        window.move(saved_ui["pet"][0], saved_ui["pet"][1])
    bubble.set_anchor(window.pos())
    if saved_ui["bubble_offset"] is not None:
        from PySide6.QtCore import QPoint

        bubble.set_offset(QPoint(saved_ui["bubble_offset"][0],
                                 saved_ui["bubble_offset"][1]))
    bubble.set_pet_width(window.width())
    window.moved.connect(lambda: bubble.set_anchor(window.pos()))  # 气泡相对跟随拖拽
    window.moved.connect(presenter.idle)   # 拖拽=交互：刷新空闲计时+从休眠唤醒

    # 立绘演出渲染循环（M1.3 状态机 → 窗口）：句级情绪/眨眼/说话状态经 tick()
    # 产出 SpriteFrame，50ms 渲染一次；图片按 key 缓存，仅换情绪时重载。
    # 修复：此前状态机从无渲染方，立绘永远停在启动时的 normal 静态图。
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QPixmap

    from client.sprite_manager import resolve_auto_pose

    pix_cache: dict = {}
    render_state = {"key": None}

    def _resolve_pose() -> str:
        """朝向解析：手动锁定优先；auto=气泡中心相对立绘中心的横向偏移。"""
        mode = pet_look["pose_mode"]
        if mode != "auto":
            return mode
        try:
            dx = (bubble.frameGeometry().center().x()
                  - window.frameGeometry().center().x())
            return resolve_auto_pose(dx)
        except Exception:
            return "stand"

    def _render_frame() -> None:
        manager.set_pose(_resolve_pose())
        frame = manager.tick()
        key = frame.image_key
        if not key:
            return
        if key == render_state["key"]:
            return
        pm = pix_cache.get(key)
        if pm is None:
            # key 相对基准目录随 manifest 回退层级走（角色包 sprites /
            # data/fallback）——恒按角色包解析会让回退层图片全部落空
            pm = QPixmap(str(manager.base_dir / key))
            pix_cache[key] = pm
        window.set_sprite(
            pm, target_height=int(pet_theme.get("base_height") or 0) or None)
        render_state["key"] = key

    def _on_look_change(key: str, value) -> None:
        """托盘「立绘」菜单：更新内存态 + 行级写回 ui_theme（保注释）。"""
        pet_look[key] = value
        if key == "outfit":
            manager.set_outfit(str(value))
        render_state["key"] = None          # 外观变化 → 强制重合成
        try:
            from client.settings_window import _rewrite_key_at_path, _yaml_scalar

            _rewrite_key_at_path(
                ROOT / "config" / "settings.yaml",
                ("ui_theme", "pet_window", key), _yaml_scalar(value))
        except Exception:
            logger.warning("立绘外观持久化失败（内存态已生效）：%s=%r",
                           key, value, exc_info=True)

    render_timer = QTimer(window)
    render_timer.timeout.connect(_render_frame)
    render_timer.start(50)

    def _apply_pet_height(height: int) -> None:
        """设置窗「界面」实时调整立绘高度（M+3.5 参数页）：立即换算 + 后续沿用。"""
        pet_theme["base_height"] = int(height)
        key = render_state["key"]
        pm = pix_cache.get(key) if key else None
        if pm is not None:
            window.set_sprite(pm, target_height=int(height) or None)

    input_bar = InputBar()
    if int(bar_theme.get("width") or 0) > 0:
        input_bar.set_width(int(bar_theme["width"]))
    if int(bar_theme.get("font_size") or 0) > 0:
        input_bar.set_font_size(int(bar_theme["font_size"]))
    _anchor_input_bar(input_bar)
    if saved_ui["bar"] is not None and point_on_screen(*saved_ui["bar"]):
        input_bar.move(saved_ui["bar"][0], saved_ui["bar"][1])

    ws = WsClient(f"ws://{host}:{port}/ws")
    playback = AudioPlayback()
    # M-CAM：摄像头感知（默认关；enabled 才开摄像头）。DND 由托盘状态源头停发。
    from client.camera_capture import CameraCapture
    from client.camera_watcher import CameraWatcher

    cam_cfg = cfg.get("camera") or {}
    camera = CameraCapture(max_side=int(cam_cfg.get("max_side", 1024)))
    cam_enabled_desired = bool(cam_cfg.get("enabled", False))
    camera_watcher = CameraWatcher(
        ws, camera,
        interval_s=float(cam_cfg.get("interval_s", 45)),
        enabled=False)          # 恢复动作延至托盘创建后（失败可见化，见下）
    # M-TOOL 工具模式（提醒域）：纯逻辑定时器 + 1s tick。播报路由定案——
    # llm.provider: api 且 WS 在线 → ToolEvent 上行（LLM 人格播报+TTS）；
    # mock/断线 → 本地固定台词气泡（永不静默丢失）。全部无视 DND（总闸 tools.enabled）。
    from client.platform.base import get_platform as _get_platform
    from client.toolbox import LOCAL_FALLBACK_LINES, Toolbox

    def _ws_connected() -> bool:
        return bool(getattr(ws, "_engine", None) and ws._engine._connected)

    def _write_alarms(entries: list) -> None:
        """闹钟列表持久化（AlarmStore on_change 回调；flow-style 列表行级写回）。"""
        from client.settings_window import _rewrite_key_at_path, _yaml_scalar

        try:
            _rewrite_key_at_path(ROOT / "config" / "settings.yaml",
                                 ("tools", "alarms"), _yaml_scalar(entries))
        except Exception:
            logger.warning("闹钟列表持久化失败（内存态已生效）", exc_info=True)

    llm_voice_on = (((cfg.get("llm") or {}).get("provider")) == "api")

    def _tool_emit(kind: str, detail: str) -> None:
        if llm_voice_on and _ws_connected():
            ws.send_text(ToolEvent(kind=kind, detail=detail).to_json())
            return
        line = LOCAL_FALLBACK_LINES.get(kind, "")
        try:
            line = line.format(detail=detail)
        except Exception:
            logger.warning("工具本地台词渲染失败 kind=%s", kind, exc_info=True)
            line = ""
        if line:
            bubble.show_sentence(line)

    tools = Toolbox(settings=cfg, clock=datetime.now,
                    idle_seconds=lambda: _get_platform().get_last_input_seconds(),
                    emit=_tool_emit, on_alarms_change=_write_alarms)
    tool_timer = QTimer(window)
    tool_timer.timeout.connect(tools.tick)
    tool_timer.start(1000)

    def _on_tool_persist(keys: tuple, value) -> None:
        """托盘工具开关的行级写回（内存态已由 Toolbox 实时生效）。"""
        from client.settings_window import _rewrite_key_at_path, _yaml_scalar

        try:
            _rewrite_key_at_path(ROOT / "config" / "settings.yaml",
                                 keys, _yaml_scalar(value))
        except Exception:
            logger.warning("工具设置持久化失败（内存态已生效）：%s=%r",
                           keys, value, exc_info=True)

    tray = TrayController(on_show=window.show, on_hide=window.hide, ws=ws,
                          bubble=bubble, input_bar=input_bar,
                          switch_entries=lambda: list_character_packages(),
                          active_package_id=lambda: active_package_id(),
                          look_getter=lambda: dict(pet_look),
                          on_look_change=lambda key, value:
                          _on_look_change(key, value),
                          toolbox=tools, on_tool_persist=_on_tool_persist)
    # 摄像头感知恢复（2026-09-12 用户需求「开关保留」收尾）：配置中已开启 →
    # 启动即恢复；打开失败必须可见（此前构造期静默失败，托盘未建无提示，
    # 用户误以为设置被重置）
    if cam_enabled_desired and not camera_watcher.set_enabled(True):
        tray.show_warning(
            "摄像头感知",
            "配置中已开启摄像头感知，但本次启动打开摄像头失败（设备被占用或"
            "未检测到）——已保持关闭，设备可用后在托盘重新打开。")
    camera_watcher.set_dnd_getter(lambda: tray.dnd)   # DND 源头停发（实时查询）
    # 托盘「静音」真实接线（此前 _muted 零消费者=纯装饰开关，勾了照样出声）：
    # 静音=不再播放语音（气泡照常）；开启瞬间 flush 已排队帧立即闭嘴
    tray.on_mute_change = lambda on: playback.flush() if on else None
    # M+2.2 平台状态上报：变更触发 + 最小间隔 5s 节流（服务端仅存内存态）
    from client.platform.base import get_platform
    from client.state_reporter import StateReporter

    state_reporter = StateReporter(get_platform(), ws.send_text, lambda: tray.dnd)
    state_reporter.start()
    # M+3.1/M+3.2 识屏：本地嗅探状态机 + 本地 OCR（默认关；OCR 缺失 → 闲置嗅探
    # 自动禁用，热键仍可手动截屏走 VLM 路）。hash/OCR 全本地，仅全帧上行。
    from client.ocr_filter import OcrFilter
    from client.screen_capture import (DEFAULT_HOTKEY, ScreenSniffer,
                                       idle_threshold_from_cfg,
                                       register_screen_hotkey)

    behavior_cfg = cfg.get("behavior") or {}
    idle_cfg = behavior_cfg.get("idle_sniff") or {}
    ocr = OcrFilter()
    sniffer = ScreenSniffer(
        send=ws.send_text, platform=get_platform(),
        ocr_run=ocr.run, ocr_ok=ocr.available,
        companion_cfg=(cfg.get("screen_triggers") or {}).get("companion"),
        enabled=bool(idle_cfg.get("enabled", False)),
        idle_threshold_s=idle_threshold_from_cfg(idle_cfg),
        connected_getter=lambda: bool(getattr(ws, "_engine", None)
                                      and ws._engine._connected),
        dnd_getter=lambda: tray.dnd,
        notify=lambda msg: tray.show_warning("识屏", msg))
    if bool(idle_cfg.get("enabled", False)) and not ocr.available():
        tray.show_warning("识屏", "未安装 OCR 组件（pip install .[ocr]），"
                                  "闲置嗅探已自动禁用；识屏热键仍可用。")
    sniff_timer = QTimer(window)
    sniff_timer.timeout.connect(sniffer.tick)
    sniff_timer.start(5000)                  # ACTIVE 态只轮询输入空闲，抓屏极低频
    hotkey_combo = str(behavior_cfg.get("screenshot_hotkey") or DEFAULT_HOTKEY)
    register_screen_hotkey(get_platform(), hotkey_combo, sniffer.handle_hotkey,
                           lambda msg: tray.show_warning("识屏热键", msg))
    ws.message_received.connect(
        # voice 必须显式传入：缺省时槽内 `voice or {}` 造出一次性字典，
        # reply_active/playback_until 写进无人读的临时对象——半双工静音门与
        # barge-in 全双工检测双双永久失效（客户端自听回环的机制根因）
        lambda raw: _on_server_message(raw, presenter, tray, playback, voice))
    # 诊断（连续拒绝/4401）走气泡：此前无人接线，客户端放弃重连时用户毫无感知
    from client.ws_client import safe_mode_hint

    ws.diagnostic.connect(lambda hint: bubble.show_sentence(hint or safe_mode_hint()))
    # 上行必须封装成协议消息（曾直接 send_text 裸文本 → 服务端 from_json 丢弃，
    # 表现「有输入无输出」，M1.10 潜伏）
    input_bar.text_submitted.connect(
        lambda text: ws.send_text(UserText(text=text).to_json()))
    # 语音连续对话（M-VOICE/VOICE.2，阶段一半双工）：🎤=对话开关，开=麦克风常开
    # VAD 自动断句上行；**半双工静音门**——reply/TTS 播放期间客户端丢帧
    # （无 AEC 防自听回环；barge-in 全双工留 M+1.5.2）
    import base64 as _b64

    import numpy as _np

    from client.audio_playback import rms_of
    from client.voice_vad import UtteranceVAD

    voice_cfg = ((cfg.get("voice") or {}).get("vad") or {})
    vad = UtteranceVAD(threshold=float(voice_cfg.get("threshold", 0.03)),
                       tail_ms=int(voice_cfg.get("tail_ms", 700)),
                       min_speech_ms=int(voice_cfg.get("min_speech_ms", 250)),
                       max_ms=int(voice_cfg.get("max_ms", 15000)),
                       tail_adaptive=bool(voice_cfg.get("tail_adaptive", True)),
                       tail_max_ms=int(voice_cfg.get("tail_max_ms", 1500)),
                       on_adapt=lambda ms: logger.info(
                           "VAD 尾点自适应 → %dms（切早则上浮，安静则回落）", ms))
    import collections as _collections

    preroll: _collections.deque = _collections.deque(maxlen=12)  # 预滚帧（~380ms）
    voice = {"hands": False, "reply_active": False,
             "playback_until": 0.0, "rate": 16000}
    # M+1.5 AEC/barge-in/系统音频防御（§4.1：extras 缺失 → 工厂降级直通 +
    # barge_in 强制 false + 托盘提示——此时维持半双工静音门）
    from client.aec_base import AecNone, create_aec
    from client.barge_in import (BargeInDetector, FarPeakTracker,
                                 SystemAudioDefense)

    aec, barge_in_enabled = create_aec(
        cfg.get("audio"),
        notify=lambda msg: tray.show_warning("语音（AEC）", msg))
    # barge-in 需真 AEC（AecNone=直通无参考，维持半双工硬门）
    barge_ready = barge_in_enabled and not isinstance(aec, AecNone)
    if barge_ready:
        playback.far_frame.connect(aec.process_far)          # 远端参考（播放线程 tee）
        playback.stream_delay.connect(aec.set_stream_delay)  # 播放端 latency 更新
    # 防御门自身贡献必须与系统混音峰值同刻度：喂远端**峰值**（真机①复盘——
    # 旧实现传 far RMS，峰值/RMS 天然差 10dB+ → 播放期恒静音门，barge-in 被饿死）
    far_peak = FarPeakTracker()
    playback.far_frame.connect(far_peak.feed)
    detector = BargeInDetector(aec_mode=barge_ready)
    defense = SystemAudioDefense()
    peak_state = {"last": -1.0, "at": 0.0}                   # 系统混音峰值 1s 节流
    mute_state = {"was": False}                              # 防御门转换诊断
    from client.platform.base import get_platform as _get_platform

    def _system_peak() -> float:
        now = time.monotonic()
        if now - peak_state["at"] >= 1.0:
            peak_state["last"] = _get_platform().get_system_audio_peak()
            peak_state["at"] = now
        return peak_state["last"]

    def _on_mic_toggled(on: bool) -> None:
        """对话开关：开=进入连续对话（等 VAD 开句）；关=整段停并收尾。"""
        from client.input_bar import write_mic_enabled

        write_mic_enabled(on)               # 🎤 状态持久化（2026-09-12 用户需求）
        voice["hands"] = on
        vad.reset()
        preroll.clear()
        if on and barge_ready:
            detector.restart_baseline()   # v1.8：进入语音模式采集 3s 环境噪声基线
        if not on:
            ws.send_text(AudioEnd().to_json())   # 保险收尾（无缓冲时服务端为 no-op）

    def _on_audio_frame(frame) -> None:
        if not voice["hands"]:
            return
        now = time.monotonic()
        playing = voice["reply_active"] or now < voice["playback_until"]
        if barge_ready:
            # M+1.5.2 AEC 档：残差检测（播放期不丢帧）；M+1.5.3 系统音频防御优先
            residual = aec.process_mic(frame)
            if playing:
                # 自身贡献 = 远端播放**峰值**（与系统混音峰值同刻度，真机①复盘）
                muted = defense.should_mute(_system_peak(), far_peak.value, True)
                if muted != mute_state["was"]:               # 转换诊断（进/出静音门各记一次）
                    mute_state["was"] = muted
                    logger.info("防御门%s：系统峰值=%.3f，自身远端峰值=%.3f",
                                "进入静音门" if muted else "解除",
                                _system_peak(), far_peak.value)
                if muted:
                    preroll.clear()
                    return                        # 他人放音频/峰值不可用 → 播放期全丢弃
                if detector.feed(rms_of(residual), True,
                                 voice.get("far_rms", 0.0)):
                    # 乐观打断：发 Interrupt 同时本地 flush 播放队列（v1.8）
                    logger.info("barge-in 触发：Interrupt + 本地 flush（残差 RMS=%.3f，"
                                "门限=%.3f，远端 RMS=%.3f）",
                                rms_of(residual), detector.last_gate or 0.0,
                                voice.get("far_rms", 0.0))
                    ws.send_text(Interrupt().to_json())
                    playback.flush()
                    voice["reply_active"] = False
                    voice["playback_until"] = 0.0
                    vad.reset()
                    preroll.clear()
                return                            # 播放期不上行（服务端在播）
        else:
            # 半双工静音门：miyako 说话期间整段丢弃（无 AEC 防自听回环）
            if playing:
                preroll.clear()
                return
        rms = rms_of(frame)
        pcm = _np.ascontiguousarray(frame, dtype=_np.int16).ravel().tobytes()
        preroll.append(pcm)                      # 常开预滚（有界，防掐头）
        event = vad.feed(rms)
        if event == "start":
            ws.send_text(AudioStart().to_json())
            for f in tuple(preroll)[:-1]:        # 预滚（不含触发帧）
                ws.send_text(AudioChunk(
                    data=_b64.b64encode(f).decode("ascii")).to_json())
        if vad.speaking:
            ws.send_text(AudioChunk(
                data=_b64.b64encode(pcm).decode("ascii")).to_json())
        if event == "end":
            ws.send_text(AudioEnd().to_json())
            preroll.clear()

    input_bar.mic_toggled.connect(_on_mic_toggled)
    input_bar.audio_frame.connect(_on_audio_frame)
    # 🎤 开关状态持久化（2026-09-12 用户需求）：上次退出时开着 → 启动即自动进入
    # 连续对话（经真实 toggled 路径，采集失败会自带回弹+禁用提示）
    if bool((cfg.get("voice") or {}).get("autostart_mic", False)):
        input_bar.mic_btn.setChecked(True)
    ws.disconnected.connect(lambda: tray.set_connected(False))
    ws.connected.connect(lambda: tray.set_connected(True))
    # 说话状态联动（M1.4 set_talking）：播放能量 >0.04 判说话、<0.02 判停（迟滞）
    talk_state = {"on": False}

    def _on_energy(rms: float) -> None:
        voice["far_rms"] = rms                  # 远端播放能量（回声泄漏门的 far 源）
        if not talk_state["on"] and rms >= 0.04:
            talk_state["on"] = True
            presenter.set_talking(True)
        elif talk_state["on"] and rms <= 0.02:
            talk_state["on"] = False
            presenter.set_talking(False)

    playback.energy.connect(_on_energy)
    if start_ws:
        ws.start()

    input_bar.show()

    def _open_history() -> None:
        """查看历史对话（M+3.5/用户需求）：数据源 = memory.db dialogue 条目，
        只读窗口（激活角色判定与设置窗/服务端同源；关窗释放连接）。

        🔑 窗口引用必须挂在 tray 上保活——PySide6 临时对象被 GC 即销毁底层
        窗口，「闪一下就没了」表现为打不开（真机 2026-09-12 定案）。
        """
        from client.history_window import HistoryWindow

        prev = getattr(tray, "_history_win", None)
        if prev is not None and prev.isVisible():
            prev.raise_()                        # 已开着 → 只是带到前台
            prev.activateWindow()
            return

        def _admin_factory():
            from server.memory.admin import MemoryAdmin
            from server.memory.store import MemoryStore

            return MemoryAdmin(MemoryStore())       # 只读：无需向量检索器

        win = HistoryWindow(admin_factory=_admin_factory)
        tray._history_win = win
        win.show()

    tray.on_history = _open_history

    def _request_countdown() -> None:
        """托盘「倒计时…」：分钟输入 → 启动 + 气泡确认。"""
        from PySide6.QtWidgets import QInputDialog

        minutes, ok = QInputDialog.getInt(None, "倒计时", "分钟数：", 10, 1, 720)
        if ok:
            tools.start_countdown(int(minutes))
            bubble.show_sentence(f"好，{int(minutes)} 分钟后叫你～")

    tray.on_countdown_request = _request_countdown

    def _open_alarm_dialog() -> None:
        """托盘「闹钟管理…」：对话框挂 tray 保活（临时对象被 GC 即闪退的教训）。"""
        from client.alarm_dialog import AlarmDialog

        prev = getattr(tray, "_alarm_dlg", None)
        if prev is not None and prev.isVisible():
            prev.raise_()
            prev.activateWindow()
            return
        dlg = AlarmDialog(toolbox=tools)
        tray._alarm_dlg = dlg
        dlg.show()

    tray.on_alarms_dialog = _open_alarm_dialog

    comp = {"app": app, "window": window, "bubble": bubble, "presenter": presenter,
            "input_bar": input_bar, "ws": ws, "tray": tray, "playback": playback,
            "camera": camera, "camera_watcher": camera_watcher,
            "state_reporter": state_reporter,
            "ocr": ocr, "sniffer": sniffer,
            "toolbox": tools, "tool_timer": tool_timer}

    def _save_ui_positions() -> None:
        """退出/重启前落盘三项位置（aboutToQuit：完全退出与切换重启共用路径）。"""
        try:
            offset = None
            if bubble.anchor is not None:
                delta = bubble.pos() - bubble.anchor
                offset = (delta.x(), delta.y())
            save_ui_positions(pet=(window.x(), window.y()),
                              bubble_offset=offset,
                              bar=(input_bar.x(), input_bar.y()))
        except Exception:
            logger.warning("UI 位置保存失败（忽略）", exc_info=True)

    app.aboutToQuit.connect(_save_ui_positions)
    # 切换编排晚绑定：handler 需要 comp 全家桶，菜单展开时才读属性
    tray.on_switch_request = _make_switch_handler(comp)

    def _open_settings() -> None:
        """设置窗（参数调整/探活/关系/记忆）。live_ui：界面组参数实时作用运行组件。"""
        from client.settings_window import SettingsWindow

        live_ui = {
            ("ui_theme", "pet_window", "base_height"): _apply_pet_height,
            ("ui_theme", "pet_window", "opacity"): window.set_opacity,
            ("ui_theme", "bubble", "scale"): bubble.set_scale,
            ("ui_theme", "bubble", "font_size"): bubble.set_font_size,
            ("ui_theme", "bubble", "width"): bubble.set_fixed_width,
            ("ui_theme", "bubble", "height"): bubble.set_fixed_height,
            ("ui_theme", "bubble", "bg_opacity"): bubble.set_bg_opacity,
            ("ui_theme", "bubble", "text_opacity"): bubble.set_text_opacity,
            ("ui_theme", "bubble", "max_sentences"): bubble.set_max_sentences,
            ("ui_theme", "input_bar", "width"):
                lambda v: input_bar.set_width(int(v)),
            ("ui_theme", "input_bar", "font_size"):
                lambda v: input_bar.set_font_size(int(v)),
            # 隐私组（实时生效）：摄像头启停含同意框；麦克风自启=恢复 🎤 状态
            ("camera", "enabled"):
                lambda v: apply_camera_enabled(
                    bool(v), watcher=camera_watcher,
                    notify=lambda m: tray.show_warning("摄像头感知", m)),
            ("voice", "autostart_mic"):
                lambda v: input_bar.mic_btn.setChecked(bool(v)),
        }
        SettingsWindow(tray=tray, live_ui=live_ui).exec()

    tray.on_settings = _open_settings
    return comp


def _make_switch_handler(comp: dict):
    """角色切换的真实副作用装配（M-CHA.4）：tray 子菜单只发请求，全流程在这里。

    时序：确认框（提示打断+重启）→ 气泡「稍等」→ 发 Interrupt → winddown
    （≤3s 等后台评估落库）→ 写 settings → 派生重启助手 + 复用「完全退出」。
    winddown 阻塞 GUI 线程至多 3s（随后即进程重启，可接受；M+1 落地
    is_settled 谓词后可提前收工）。
    """
    from PySide6.QtWidgets import QApplication, QMessageBox

    from client.switcher import (SwitchSteps, list_character_packages,
                                 perform_switch, schedule_restart,
                                 write_active_package_id)
    from server.constants import CHARACTERS_DIR, active_package_id, validate_character_package
    from server.persona import display_name_from_package

    bubble = comp["bubble"]
    ws = comp["ws"]
    tray = comp["tray"]

    def handle(target_id: str) -> None:
        target_pkg = CHARACTERS_DIR / f"{target_id}.char"

        def confirm() -> bool:
            reply = QMessageBox.question(
                None, "切换角色",
                f"切换到「{display_name_from_package(target_pkg)}」将打断当前对话"
                f"并重启 miyako，确定吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            return reply == QMessageBox.StandardButton.Yes

        steps = SwitchSteps(
            is_valid=lambda pid: validate_character_package(
                CHARACTERS_DIR / f"{pid}.char") is None,
            confirm=confirm,
            # WsClient 只有 send_text（send 是 WsEngine 的方法）——
            # 曾写 ws.send → AttributeError 被 Qt 槽吞掉，切换永远停在「稍等」
            interrupt=lambda: ws.send_text(Interrupt().to_json()),
            write_settings=write_active_package_id,
            restart=lambda: (schedule_restart(), tray.quit_all()),
            on_reject=lambda pid: tray.show_warning(
                "角色切换失败",
                f"角色包 {pid} 校验失败，已保持当前角色不变。"),
        )

        def notify_settling() -> None:
            bubble.show_sentence("稍等，把这句记完…马上回来～")
            QApplication.processEvents()   # 阻塞收尾窗前让气泡先画出来

        outcome = perform_switch(target_id, steps, before_winddown=notify_settling)
        if outcome != "ok":
            logger.warning("切换未完成（%s）：%r", outcome, target_id)
    return handle


def _anchor_input_bar(bar) -> None:
    """输入栏固定在主屏左下角（可拖立绘之外的常驻交互面，M+3.5 设置窗再开放配置）。"""
    from PySide6.QtWidgets import QApplication

    screen = QApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    bar.adjustSize()
    bar.move(geo.left() + 24, geo.bottom() - bar.height() - 24)


def run_app() -> int:
    from server.constants import load_settings   # settings 读取唯一入口（卫生批次③）

    # 日志先于组件装配：pythonw 无控制台，client.log 是唯一运行日志出口
    setup_logging("client")
    cfg = load_settings()
    comp = build_components(cfg)
    return comp["app"].exec()


def _load_sprite_pixmap(manager):
    """按当前情绪解析图片相对路径并加载为 QPixmap；缺失返回 None（透明窗口）。"""
    from PySide6.QtGui import QPixmap

    key = manager.image_key("normal")
    if not key:
        return None
    pixmap = QPixmap(str(manager.base_dir / key))
    if pixmap.isNull():
        logger.warning("立绘 %s 加载失败（M1.11 生成后恢复正常）", key)
        return None
    return pixmap


def _on_server_message(raw: str, presenter, tray, playback=None,
                        voice: Optional[dict] = None) -> None:
    msg = from_json(raw)
    voice = voice or {}
    if isinstance(msg, Sentence):
        if voice:
            voice["reply_active"] = True
            voice["playback_until"] = max(
                voice["playback_until"], time.monotonic() + 1.0)
        presenter.set_emotion(msg.emotion)
        presenter.show_bubble(msg.text_zh)
        if msg.motion:
            presenter.play_motion(msg.motion)
    elif isinstance(msg, AudioBegin):
        if playback is not None:
            # 采样率跟踪无条件执行（静音期间也要换轨）：否则解除静音后新率
            # 音频块写进旧率流=变调。静音只闭「写帧」。
            playback.ensure_sample_rate(msg.sample_rate)
        if voice:
            voice["rate"] = msg.sample_rate
            voice["reply_active"] = True
    elif isinstance(msg, AudioChunk):
        if playback is not None and not tray.muted:
            import base64

            import numpy as np
            pcm = np.frombuffer(base64.b64decode(msg.data), dtype="<i2").copy()
            playback.write(pcm)
            if voice:
                # 播放静音门估算：本帧播完时刻之前，麦克风半双工丢弃
                dur = len(pcm) / 2.0 / max(1, playback.sample_rate)
                voice["playback_until"] = max(
                    voice["playback_until"],
                    time.monotonic() + dur + 0.25)
    elif isinstance(msg, ReplyEnd):
        if voice:
            voice["reply_active"] = False
    elif hasattr(msg, "scope"):  # Error：托盘 TTS 降级提示
        tray.report_tts_degradation(msg.scope, msg.fallback)


if __name__ == "__main__":
    sys.exit(guarded_main())
