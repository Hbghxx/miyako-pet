"""托盘（M1.10）：显示/隐藏、静音、DND、设置占位、TTS 降级提示、退出子菜单。

退出（2026-09-12 用户需求改版）拆两档：
  · 「完全退出（同时关闭 TTS）」= 结束客户端 + kill server 子进程（pid 取自
    环境变量 MIYAKO_SERVER_PID，其次 data/server.pid）+ 按监听端口定位并关闭
    独立常驻的 GPT-SoVITS TTS 服务（默认 9880，由启动 bat 最小化拉起）；
  · 「仅关闭 miyako（保留 TTS 服务）」= 原完全退出行为（TTS 模型常驻显存，
    下次启动免重复加载；切换角色重启路径也走这档，避免换角色重载模型）。
无 python 残留（M1 手动清单第 ⑦ 项）。
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (QApplication, QMessageBox, QMenu, QSystemTrayIcon,
                               QStyle)

from server.constants import APP_ROOT as REPO_ROOT
from server.persona import display_name

logger = logging.getLogger(__name__)

PID_FILE = REPO_ROOT / "data" / "server.pid"


def tooltip_text(connected: bool | None = None,
                 name: str | None = None) -> str:
    """托盘 tooltip 文案（M-CHA.2）：角色展示名 + 可选连接状态。纯函数可测。

    connected=None 表示装配初期尚无连接状态可显示，只出名字。
    """
    name = name if name is not None else display_name()
    if connected is None:
        return name
    return f"{name}（{'在线' if connected else '连接断开，重连中…'}）"


def read_server_pid(env: Optional[dict[str, str]] = None,
                    pid_file: Path = PID_FILE) -> Optional[int]:
    """server pid：环境变量优先（启动 bat 注入），其次 pid 文件。"""
    env = env if env is not None else os.environ
    raw = env.get("MIYAKO_SERVER_PID", "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        if pid_file.is_file():
            raw = pid_file.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                return int(raw)
    except OSError:
        logger.debug("pid 文件读取失败", exc_info=True)
    return None


def kill_server_pid(pid: int) -> bool:
    """终止 server 进程。Windows 的 os.kill 对任意 sig 等价 TerminateProcess。"""
    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("已终止 server 子进程 pid=%s", pid)
        return True
    except ProcessLookupError:
        logger.info("server pid=%s 已不存在", pid)
        return True   # 目标已消失视为成功
    except PermissionError as exc:
        logger.warning("无权限终止 server pid=%s：%s", pid, exc)
        return False
    except OSError as exc:
        logger.warning("终止 server pid=%s 失败：%s", pid, exc)
        return False


def stop_server(env: Optional[dict[str, str]] = None,
                pid_file: Path = PID_FILE) -> bool:
    """完全退出的 server 侧：找到 pid → kill → 清理 pid 文件。"""
    pid = read_server_pid(env=env, pid_file=pid_file)
    if pid is None:
        logger.info("未找到 server pid（server 可能未由本机脚本启动）")
        return False
    ok = kill_server_pid(pid)
    try:
        if pid_file.is_file():
            pid_file.unlink()
    except OSError:
        logger.debug("pid 文件清理失败", exc_info=True)
    return ok


# ---------------- TTS 服务查杀（2026-09-12 用户需求：完全退出时一并关闭） ----------------

TTS_DEFAULT_PORT = 9880   # 与 server.providers.tts_gptsovits.API_URL_DEFAULT 同源缺省


def find_pid_listening_on(port: int, *, netstat_fn=None) -> Optional[int]:
    """在 netstat -ano 里找监听 ``:<port>`` 的 PID（无监听/查询失败 → None）。

    输出按字节捕获后容错解码——中文 Windows 控制台输出是本地代码页（GBK），
    ``text=True`` 的 UTF-8 解码在读取线程直接炸（tests/e2e test_release 同款坑）。
    """
    fn = netstat_fn or subprocess.run
    try:
        proc = fn(["netstat", "-ano"], capture_output=True, timeout=10)
        text = proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("netstat 查询失败，无法定位端口 %s 的服务 pid", port)
        return None
    suffix = f":{port}"
    for line in text.splitlines():
        parts = line.split()
        # 形如：TCP  127.0.0.1:9880  0.0.0.0:0  LISTENING  31980
        if (len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING"
                and parts[1].endswith(suffix)):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def stop_tts_service(port: int = TTS_DEFAULT_PORT) -> bool:
    """结束 TTS 服务进程（按监听端口定位；未运行 → False，失败仅日志不阻断退出）。"""
    pid = find_pid_listening_on(port)
    if pid is None:
        logger.info("TTS 服务（端口 %s）未在监听，无需关闭", port)
        return False
    try:
        os.kill(pid, signal.SIGTERM)   # Windows 等价 TerminateProcess（同 kill_server_pid）
        logger.info("已终止 TTS 服务 pid=%s（端口 %s）", pid, port)
        return True
    except ProcessLookupError:
        logger.info("TTS 服务 pid=%s 已不存在", pid)
        return False
    except OSError as exc:
        logger.warning("终止 TTS 服务 pid=%s 失败：%s", pid, exc)
        return False


class TrayController(QSystemTrayIcon):
    """系统托盘。完全退出：ws.stop() → stop_server() → QApplication.quit()。

    切换角色子菜单（M-CHA.4）：枚举 *.char 读 display_name，激活者打勾；
    实际切换编排由 run_client 注入的 on_switch_request 承接（tray 保持薄壳）。
    """

    def __init__(self, *, parent=None,
                 on_show: Optional[Callable[[], None]] = None,
                 on_hide: Optional[Callable[[], None]] = None,
                 ws=None,
                 bubble=None,
                 input_bar=None,
                 switch_entries: Optional[Callable[[], list]] = None,
                 active_package_id: Optional[Callable[[], str]] = None,
                 on_switch_request: Optional[Callable[[str], None]] = None,
                 on_settings: Optional[Callable[[], None]] = None,
                 look_getter: Optional[Callable[[], dict]] = None,
                 on_look_change: Optional[Callable[[str, object], None]] = None,
                 toolbox=None,
                 on_tool_persist: Optional[Callable[[tuple, object], None]] = None) -> None:
        self.on_show = on_show or (lambda: None)
        self.on_hide = on_hide or (lambda: None)
        self.ws = ws
        self._bubble = bubble            # 气泡/输入框细分显隐（2026-09 用户需求）
        self._input_bar = input_bar
        self._switch_entries = switch_entries or (lambda: [])
        self._active_package_id = active_package_id or (lambda: "")
        self.on_switch_request = on_switch_request or (lambda pid: None)
        self.on_settings = on_settings          # M+2.4 设置窗口（run_client 晚绑定）
        self._look_getter = look_getter or (lambda: {})          # M-SPRITE 立绘菜单
        self._on_look_change = on_look_change or (lambda key, value: None)
        self._toolbox = toolbox                  # M-TOOL 工具菜单（None=无该模块）
        self._on_tool_persist = on_tool_persist or (lambda keys, value: None)
        self.on_history: Optional[Callable[[], None]] = None  # 查看历史对话（run_client 晚绑定）
        self.on_countdown_request: Optional[Callable[[], None]] = None  # 倒计时输入框（晚绑定）
        self.on_alarms_dialog: Optional[Callable[[], None]] = None      # 闹钟管理对话框（晚绑定）
        # 静音真实接线（run_client 晚绑定）：此前 _muted 零消费者，开关是纯装饰
        self.on_mute_change: Optional[Callable[[bool], None]] = None
        super().__init__(self._make_icon(), parent)
        self.setToolTip(tooltip_text())
        self._muted = False
        self._dnd = False
        self._card_import_dialog = None     # 一键配置对话框保活引用（防 GC 闪退）
        self._build_menu()
        # QSystemTrayIcon 必须显式 show，否则图标不可见（M1 手动清单⑤⑦实测暴露：
        # 此前构造后从未 show，托盘自 M1.10 起从未真正出现过）
        self.show()

    @staticmethod
    def _make_icon() -> QIcon:
        style = QApplication.style()
        if style is not None:
            return style.standardIcon(QStyle.SP_ComputerIcon)
        return QIcon()

    def _build_menu(self) -> None:
        menu = QMenu()
        # UI 子菜单：气泡/输入框/角色立绘 三个显隐开关（默认开，点击切换；
        # 2026-09 用户需求——合并原 显示/隐藏 两项）
        menu_ui = QMenu("UI", menu)
        if self._bubble is not None:
            act = menu_ui.addAction("气泡")
            act.setCheckable(True)
            act.setChecked(True)
            act.triggered.connect(lambda checked: self._toggle_ui("bubble", checked))
        if self._input_bar is not None:
            act = menu_ui.addAction("输入框")
            act.setCheckable(True)
            act.setChecked(True)
            act.triggered.connect(lambda checked: self._toggle_ui("input", checked))
        act = menu_ui.addAction("角色立绘")
        act.setCheckable(True)
        act.setChecked(True)
        act.triggered.connect(lambda checked: self._toggle_ui("pet", checked))
        menu.addMenu(menu_ui)
        self._act_mute = QAction("静音", menu)
        self._act_mute.setCheckable(True)
        self._act_mute.toggled.connect(self._toggle_mute)
        self._act_dnd = QAction("勿扰模式（DND）", menu)
        self._act_dnd.setCheckable(True)
        self._act_dnd.toggled.connect(self._toggle_dnd)
        act_settings = QAction("设置…", menu)
        act_settings.triggered.connect(self._settings_placeholder)
        act_update = QAction("检查更新…", menu)
        act_update.triggered.connect(self._run_update_check)
        act_history = QAction("查看历史对话…", menu)
        act_history.triggered.connect(self._open_history)
        self._switch_menu = QMenu("切换角色", menu)
        self._switch_menu.aboutToShow.connect(self._rebuild_switch_menu)
        self._act_tts_hint = QAction("TTS：正常", menu)
        self._act_tts_hint.setEnabled(False)
        self._look_menu = QMenu("立绘", menu)
        self._look_menu.aboutToShow.connect(self._rebuild_look_menu)
        self._tools_menu = QMenu("工具", menu)
        self._tools_menu.aboutToShow.connect(self._rebuild_tools_menu)
        exit_menu = QMenu("退出", menu)
        act_quit_full = QAction("完全退出（同时关闭 TTS）", exit_menu)
        act_quit_full.triggered.connect(self.quit_all_with_tts)
        act_quit_soft = QAction("仅关闭 miyako（保留 TTS 服务）", exit_menu)
        act_quit_soft.triggered.connect(self.quit_all)
        exit_menu.addAction(act_quit_full)
        exit_menu.addAction(act_quit_soft)
        for act in (self._act_mute, self._act_dnd,
                    act_settings, act_history, act_update, self._act_tts_hint):
            menu.addAction(act)
        act_import = menu.addAction("一键配置角色包…")
        act_import.triggered.connect(self._open_card_import)
        act_tavern = menu.addAction("导入酒馆卡…")
        act_tavern.triggered.connect(self._open_tavern_import)
        menu.addMenu(self._switch_menu)
        menu.addMenu(self._look_menu)
        menu.addMenu(self._tools_menu)
        menu.addSeparator()
        menu.addMenu(exit_menu)
        self.setContextMenu(menu)

    def _open_card_import(self) -> None:
        """一键配置角色包（2026-09-17 用户需求）：选文件夹导入卡+接线 TTS+激活。

        对话框挂 self 保活（history_window 同款教训：临时对象被 Qt 引用计数
        回收=闪一下即消失）。重启回调 = 完全退出（含 TTS，服务重启吃新 yaml）
        + schedule_restart 重拉启动 bat（复用切换角色的重启编排）。
        """
        if getattr(self, "_card_import_dialog", None) is not None:
            self._card_import_dialog.showNormal()
            self._card_import_dialog.raise_()
            self._card_import_dialog.activateWindow()
            return

        def restart_effective() -> None:
            self.quit_all_with_tts()
            from client.switcher import schedule_restart

            schedule_restart()

        from client.card_import_dialog import CardImportDialog

        self._card_import_dialog = CardImportDialog(
            restart_callback=restart_effective,
            parent=None)
        self._card_import_dialog.show()

    def _open_tavern_import(self) -> None:
        """导入酒馆卡（2026-09-18 用户需求）：SillyTavern .json/.png → miyako 包。

        与一键配置平行的独立模式（原卡链路零改动）；音色=借用当前激活包
        参考音频池（恒定，无开关）。对话框挂 self 保活（同 _open_card_import）。
        """
        if getattr(self, "_tavern_import_dialog", None) is not None:
            self._tavern_import_dialog.showNormal()
            self._tavern_import_dialog.raise_()
            self._tavern_import_dialog.activateWindow()
            return

        def restart_effective() -> None:
            self.quit_all_with_tts()
            from client.switcher import schedule_restart

            schedule_restart()

        from client.tavern_import_dialog import TavernImportDialog

        self._tavern_import_dialog = TavernImportDialog(
            restart_callback=restart_effective,
            parent=None)
        self._tavern_import_dialog.show()

    def _rebuild_switch_menu(self) -> None:
        """每次展开现枚举（激活者打勾；包的增删改即时可见）。"""
        self._switch_menu.clear()
        active = self._active_package_id()
        for entry in self._switch_entries():
            act = self._switch_menu.addAction(entry.display_name)
            act.setCheckable(True)
            act.setChecked(entry.package_id == active)
            act.setEnabled(entry.package_id != active)
            act.triggered.connect(
                lambda _=False, target=entry.package_id: self.on_switch_request(target))

    def _rebuild_look_menu(self) -> None:
        """立绘菜单（M-SPRITE 换装/朝向）：每次展开现读现建（打勾即当前值）。"""
        m = self._look_menu
        m.clear()
        look = self._look_getter()
        outfits_menu = QMenu("服装", m)
        for label, key in (("私服", "casual"), ("制服", "uniform"),
                           ("女仆装", "waitress"), ("连帽衫", "parka"),
                           ("裸", "naked")):
            act = outfits_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(look.get("outfit") == key)
            act.triggered.connect(
                lambda _=False, k=key: self._on_look_change("outfit", k))
        m.addMenu(outfits_menu)
        poses_menu = QMenu("朝向", m)
        for label, key in (("自动（跟随气泡）", "auto"), ("正面", "stand"),
                           ("左向", "left"), ("右向", "right")):
            act = poses_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(look.get("pose_mode") == key)
            act.triggered.connect(
                lambda _=False, k=key: self._on_look_change("pose_mode", k))
        m.addMenu(poses_menu)

    def _rebuild_tools_menu(self) -> None:
        """工具菜单（M-TOOL）：每次展开现读现建（打勾即当前值）。

        全部无视 DND 定案：本菜单不受 DND 影响；总闸=tools.enabled。
        开关类动作同时实时改 Toolbox 内存态 + 行级写回 settings（_on_tool_persist）。
        """
        m = self._tools_menu
        m.clear()
        tb = self._toolbox
        if tb is None:
            act = m.addAction("工具模块未装配")
            act.setEnabled(False)
            return

        act_master = m.addAction("工具模式总闸")
        act_master.setCheckable(True)
        act_master.setChecked(tb.enabled)
        act_master.triggered.connect(lambda on: self._tool_switch_master(on))
        m.addSeparator()

        if tb.pomodoro.active:
            act_pomo = m.addAction(tb.pomodoro_label() or "停止番茄钟")
            act_pomo.triggered.connect(tb.stop_pomodoro)
        else:
            act_pomo = m.addAction(f"开始番茄钟（{tb.pomodoro.work_min} 分钟）")
            act_pomo.triggered.connect(tb.start_pomodoro)
        if tb.countdown.active:
            from datetime import datetime

            remain_min = max(1, tb.countdown.remaining_seconds(datetime.now()) // 60)
            act_cd = m.addAction(f"停止倒计时（剩 {remain_min} 分钟）")
            act_cd.triggered.connect(tb.stop_countdown)
        else:
            act_cd = m.addAction("倒计时…")
            act_cd.triggered.connect(self._on_tool_countdown)
        m.addAction("闹钟管理…").triggered.connect(self._on_tool_alarms)
        m.addSeparator()
        for label, getter, setter, keys, on_value, off_value in (
                ("喝水提醒", lambda: tb.reminders.water_min > 0,
                 tb.set_water_enabled, ("tools", "water_min"),
                 tb.default_water_min, 0),
                ("久坐提醒", lambda: tb.reminders.sedentary_min > 0,
                 tb.set_sedentary_enabled, ("tools", "sedentary_min"),
                 tb.default_sedentary_min, 0),
                ("整点报时", lambda: tb.chime.enabled,
                 tb.set_chime_enabled, ("tools", "hourly", "enabled"), True, False),
                ("节日提醒", lambda: tb.festivals.enabled,
                 tb.set_festivals_enabled, ("tools", "festivals", "enabled"),
                 True, False)):
            act = m.addAction(label)
            act.setCheckable(True)
            act.setChecked(getter())
            act.triggered.connect(
                lambda on, s=setter, k=keys, v_on=on_value, v_off=off_value:
                (s(on), self._on_tool_persist(k, v_on if on else v_off)))

    def _tool_switch_master(self, on: bool) -> None:
        """总闸切换：内存态实时生效 + 写回 settings。"""
        if self._toolbox is not None:
            self._toolbox.set_enabled(on)
        self._on_tool_persist(("tools", "enabled"), bool(on))

    def _on_tool_countdown(self) -> None:
        cb = getattr(self, "on_countdown_request", None)
        if cb is not None:
            cb()

    def _on_tool_alarms(self) -> None:
        cb = getattr(self, "on_alarms_dialog", None)
        if cb is not None:
            cb()

    def _toggle_mute(self, on: bool) -> None:
        self._muted = on
        logger.info("静音=%s", on)
        if self.on_mute_change is not None:
            try:
                self.on_mute_change(on)
            except Exception:
                logger.warning("静音回调异常（不影响开关状态）", exc_info=True)

    def _toggle_ui(self, key: str, checked: bool) -> None:
        """UI 子菜单开关（默认开，点击切换）。气泡为粘性隐藏：关=后续回复不弹窗。"""
        if key == "bubble" and self._bubble is not None:
            self._bubble.auto_show = checked
            (self._bubble.show if checked else self._bubble.hide)()
        elif key == "input" and self._input_bar is not None:
            (self._input_bar.show if checked else self._input_bar.hide)()
        elif key == "pet":
            (self.on_show if checked else self.on_hide)()
        else:
            logger.debug("UI 开关 %s 无对应组件（未注入），忽略", key)

    def _toggle_dnd(self, on: bool) -> None:
        self._dnd = on
        logger.info("DND=%s（behavior gate M+2.3 消费）", on)
        # 切换可见性（2026-09-17 事故：DND 被无声打开连陪玩一起杀，用户零感知）
        self.show_warning(
            "勿扰模式",
            "已开启：主动搭话/陪玩吐槽/闲置嗅探全部暂停" if on
            else "已关闭：主动搭话与识屏两域恢复")

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def dnd(self) -> bool:
        return self._dnd

    def _settings_placeholder(self) -> None:
        """M+2.4：打开设置窗口基础版（on_settings 由 run_client 晚绑定）；
        未注入时退回模态信息框（M+3.5 完整设置窗前保底）。"""
        if self.on_settings is not None:
            self.on_settings()
            return
        QMessageBox.information(
            None, f"{display_name()} 设置",
            "完整设置窗口在 M+3.5 提供。\n\n"
            "当前可手动调整（改后重启 miyako 生效）：\n"
            "  · config/settings.yaml → ui_theme 节：气泡缩放(scale)、"
            "立绘高度(base_height)、输入栏尺寸\n"
            "  · 同文件 providers 节：语音（gptsovits/edge）、ASR\n"
            "  · 托盘菜单：显示/隐藏各界面元素、静音、勿扰、切换角色")

    def report_tts_degradation(self, scope: str, fallback: str) -> None:
        """Error{scope:tts} 时更新托盘提示（TTS 降级级别提示）。"""
        self._act_tts_hint.setText(f"TTS 降级：{scope} → {fallback}")

    def _open_history(self) -> None:
        """查看历史对话（M+3.5/用户需求）。on_history 由 run_client 注入装配。

        失败必须可见：pythonw 无控制台，静默吞掉 = 用户点了没反应且无从排查
        （2026-09-12 真机「打不开」排障教训）。
        """
        try:
            self.on_history()
        except Exception as exc:
            logger.warning("打开历史对话窗口失败", exc_info=True)
            QMessageBox.warning(
                None, "无法打开历史对话",
                f"错误：{exc!r}\n\n详情见日志；请截图此提示反馈。")

    # ---------------- 检查更新（M+3.5.3；唯一外发面，见 README 透明度声明） ----------------
    def _run_update_check(self) -> None:
        """后台线程跑 GET UPDATE_URL（GUI 线程只弹结果框）。"""
        from client.updater import check_for_update
        from client.settings_window import _read_yaml_path

        url = str(_read_yaml_path(None, ("update", "url"), "") or "")
        prev = getattr(self, "_update_thread", None)
        if prev is not None and prev.isRunning():
            # 运行中线程被替换 → Python 侧引用脱落 → QThread 运行中被 GC =
            # qFatal 进程中止（「QThread: Destroyed while thread is still running」）
            logger.info("更新检查进行中，忽略重复触发")
            return

        class _CheckThread(QThread):
            finished_with = Signal(tuple)

            def run(self_) -> None:
                self_.finished_with.emit(check_for_update(url))

        # 挂 self 为父对象：托盘终身持有，finished 线程也不再被 GC 提前回收
        self._update_thread = _CheckThread(self)
        self._update_thread.finished_with.connect(self._on_update_result)
        self._update_thread.start()

    def _on_update_result(self, result: tuple) -> None:
        status, detail = result
        if status == "unconfigured":
            QMessageBox.information(
                None, "检查更新",
                "发布者尚未配置检查更新地址（settings.yaml 的 update.url）。")
        elif status == "update":
            answer = QMessageBox.information(
                None, "发现新版本",
                f"检测到新版本：v{detail}（当前 v{self._local_version_for_ui()}）。\n\n"
                "升级方法：\n"
                "1. 下载新版 zip，覆盖解压到安装目录（data 文件夹勿删）\n"
                "2. 重新运行一次 install.bat\n"
                "3. 双击 启动miyako.bat",
                QMessageBox.StandardButton.Ok)
            _ = answer
        elif status == "latest":
            QMessageBox.information(None, "检查更新",
                                    f"已是最新版本（v{detail}）。")
        else:
            QMessageBox.warning(None, "检查更新",
                                f"检查失败（{detail}）：网络不可达或地址无效。\n"
                                "不影响其它功能使用。")

    @staticmethod
    def _local_version_for_ui() -> str:
        from client.updater import local_version

        return local_version()

    def show_warning(self, title: str, message: str) -> None:
        """托盘 ⚠️（M-CHA.4：坏包拒绝切换等）。显示失败仅记日志（热键注册
        失败等通知路径不得反噬装配链，M+3.1 冒烟实测暴露）。"""
        try:
            self.showMessage(title, message, QSystemTrayIcon.MessageIcon.Warning)
        except Exception:
            logger.warning("托盘提示显示失败：%s %s", title, message, exc_info=True)

    def set_connected(self, connected: bool) -> None:
        """连接状态可视化：断线灰、在线绿（图标区分，M1 手动清单第⑤项）。"""
        self.setToolTip(tooltip_text(connected))

    def quit_all(self) -> None:
        logger.info("完全退出：停止 client 组件并终止 server 子进程")
        try:
            if self.ws is not None:
                self.ws.stop()
        except Exception:
            logger.debug("ws 停止异常", exc_info=True)
        stop_server()
        QApplication.quit()

    def quit_all_with_tts(self) -> None:
        """「完全退出（同时关闭 TTS）」：先关独立常驻的 TTS 服务再走常规退出。

        TTS 未在运行/关闭失败不阻断退出（仅日志）——切角色重启复用的 quit_all
        保持不碰 TTS，模型常驻免重复加载的取舍只在显式完全退出时打破。
        """
        try:
            stop_tts_service()
        except Exception:
            logger.warning("关闭 TTS 服务异常（继续退出 miyako）", exc_info=True)
        self.quit_all()
