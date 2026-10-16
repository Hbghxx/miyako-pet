"""设置窗口（M+3.5 + 参数调整扩展）—— 参数 / Providers / 关系 / 记忆 / Token 监控。

「参数调整」页（用户 2026-09-11 需求）：settings.yaml 全部可调参数集中呈现，
按分组声明式生成（_PARAMS 表）：
  - 「界面」组**实时生效**：改动即通过 live_ui 回调作用于运行中的组件
    （立绘高度/气泡缩放/输入框宽字号），应用时写入 yaml；
  - 其余组**重启生效**（服务端/客户端在装配时读一次）：应用时统一列出
    本次改动项，二次询问「立即重启」（复用角色切换的重启助手：等服务端
    pid 消失后重拉启动脚本，全端重启）。

写入一律走 `_rewrite_key_at_path` 行级保注释改写（键/节缺失则按 2 空格
缩进创建，settings.example 的既有行优先生效）；不做整体 safe_dump（会丢光
注释）。记忆/关系操作经 server/memory/admin.py（SQL 纪律落点在 store/vector）。

「Token 监控」页（用户 2026-09-12 需求③）：轮询 server /metrics 展示分角色
token 消耗、上下文缓存命中（cached_tokens，验证 Coding Plan 缓存收益）、
成本段与跨重启累计（data/memory/{角色}.usage.json）。窗口隐藏即停轮询。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSpinBox, QTabWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from server.constants import SETTINGS_PATH, load_settings

logger = logging.getLogger(__name__)

_IDLE_SNIFF_BLURB = ("闲置嗅探：你一段时间没动电脑后，miyako 会在本地「看一眼」屏幕"
                     "（只算画面变化指纹，不上传）；等你回来时，若屏幕内容命中你配置的"
                     "规则（如报错/通关字样），才会截屏发起关心。每 4 小时最多一次。")

# ---------------------------------------------------------------- 行级嵌套键写入


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _name_of(line: str) -> str:
    return line.split(":", 1)[0].strip()


def _is_section(line: str) -> bool:
    """节行：冒号后（去掉行内注释）无标量值。"""
    rest = line.split(":", 1)[1] if ":" in line else ""
    cut = rest.find(" #")
    if cut >= 0:
        rest = rest[:cut]
    return rest.strip() == ""


def _locate_block(lines: list[str], chain: list[str]) -> Optional[tuple[int, int, int]]:
    """定位节链：返回 (末节行 idx, 末节缩进, 块尾 idx[不含])。链不完整 → None。"""
    starts: list[int] = []
    indents: list[int] = []
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = _indent_of(line)
        while indents and ind <= indents[-1]:
            indents.pop()
            starts.pop()
        if len(indents) < len(chain) and _is_section(line) \
                and _name_of(line) == chain[len(indents)] \
                and (not indents or ind > indents[-1]):
            indents.append(ind)
            starts.append(i)
            if len(indents) == len(chain):
                for j in range(i + 1, len(lines)):
                    probe = lines[j]
                    if not probe.strip() or probe.lstrip().startswith("#"):
                        continue
                    if _indent_of(probe) <= ind:
                        return starts[-1], ind, j
                return starts[-1], ind, len(lines)
    return None


def _rewrite_key_at_path(settings_path: Path | None, keys: tuple[str, ...],
                         value_text: str) -> bool:
    """行级保注释改写/创建嵌套键（如 ("behavior","idle_sniff","enabled")）。

    键已存在 → 原位改值（保行内注释）；键缺 → 在所属节块尾按 2 空格缩进
    插入；节缺 → 从缺失层级起整体追加到文件尾。返回是否发生了写入。
    """
    path = Path(settings_path) if settings_path is not None else SETTINGS_PATH
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = text.splitlines()
    chain = list(keys[:-1])
    key = keys[-1]
    loc = _locate_block(lines, chain)
    if loc is not None:
        sec_idx, sec_indent, block_end = loc
        for i in range(sec_idx + 1, block_end):
            line = lines[i]
            if line.lstrip().startswith("#") or not line.strip():
                continue
            if _indent_of(line) > sec_indent and _name_of(line) == key:
                comment = ""
                m = re.search(r"\s+#.*$", line)
                if m:
                    comment = m.group(0).rstrip()
                lines[i] = f"{' ' * _indent_of(line)}{key}: {value_text}{comment}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                from server.constants import invalidate_text_cache

                invalidate_text_cache(path)   # 读缓存失效契约：读后写立即可见
                return True
        indent = sec_indent + 2
        lines.insert(block_end, f"{' ' * indent}{key}: {value_text}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        from server.constants import invalidate_text_cache

        invalidate_text_cache(path)
        return True
    # 节链缺失：在最后一个已匹配层级之后续建；整链都缺 → 文件尾追加
    partial: list[int] = []
    partial_indents: list[int] = []
    if chain:
        for depth in range(len(chain), 0, -1):
            partial_loc = _locate_block(lines, chain[:depth])
            if partial_loc is not None:
                _, sec_indent, block_end = partial_loc
                partial = [block_end]
                base_indent = sec_indent
                for extra in chain[depth:]:
                    base_indent += 2
                    partial.append(f"{' ' * base_indent}{extra}:")
                break
    if not partial:
        if lines and lines[-1].strip():
            lines.append("")
        base_indent = -2
        for extra in chain:
            base_indent += 2
            lines.append(f"{' ' * base_indent}{extra}:")
        lines.append(f"{' ' * (base_indent + 2)}{key}: {value_text}")
    else:
        insert_at = partial[0]
        new_lines = partial[1:] + [f"{' ' * (base_indent + 2)}{key}: {value_text}"]
        for offset, line in enumerate(new_lines):
            lines.insert(insert_at + offset, line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _yaml_scalar(value) -> str:
    """Python 值 → YAML 标量/flow 文本（bool/int/float/null/字符串/list）。

    safe_dump 对纯标量文档会包 `---`/`...` 文档标记（None 尤其如此），
    必须剥掉——混进行值会把整份配置写坏（2026-09-11 参数页实测踩坑）。
    """
    import yaml

    text = yaml.safe_dump(value, default_flow_style=True,
                          allow_unicode=True).strip()
    if text.startswith("---"):
        text = text[3:].strip()
    if text.endswith("..."):
        text = text[:-3].strip()
    return text


# ---------------------------------------------------------------- 读取辅助


def _read_yaml_path(settings_path: Path | None, keys: tuple[str, ...], default):
    data = load_settings(settings_path)   # 唯一入口（参数=测试注入路径，缺省生产文件）
    for key in keys:
        data = (data or {}).get(key) if isinstance(data, dict) else None
    return default if data is None else data


def read_behavior_idle_sniff(settings_path: Path | None = None) -> bool:
    return bool(_read_yaml_path(settings_path,
                                ("behavior", "idle_sniff", "enabled"), False))


def read_screenshot_hotkey(settings_path: Path | None = None) -> str:
    return str(_read_yaml_path(settings_path,
                               ("behavior", "screenshot_hotkey"), "") or "")


def set_behavior_idle_sniff(enabled: bool, settings_path: Path | None = None) -> bool:
    """行级保注释改写 behavior.idle_sniff.enabled（设置窗行为组共用实现）。"""
    return _rewrite_key_at_path(settings_path, ("behavior", "idle_sniff", "enabled"),
                                _yaml_scalar(bool(enabled)))


def set_behavior_screenshot_hotkey(combo: str, settings_path: Path | None = None) -> bool:
    return _rewrite_key_at_path(settings_path, ("behavior", "screenshot_hotkey"),
                                _yaml_scalar(str(combo)))


# ---------------------------------------------------------------- 参数表


def _wl_decode(value) -> str:
    """yaml list → 逗号分隔文本（fullscreen_whitelist 编辑框用）。"""
    return ", ".join(str(x) for x in (value or []))


def _wl_encode(text: str) -> list[str]:
    # 全角标点归一（2026-09-12 真机陷阱：中文 IME 的「，」「：」导致名单整串
    # 并成单条目 → 解析整条非法静默跳过）
    normalized = str(text).replace("，", ",").replace("：", ":")
    return [s.strip() for s in normalized.split(",") if s.strip()]


@dataclass
class Param:
    """单条参数：分组/路径/控件/默认值；live=True 的改动实时作用运行组件。"""

    group: str
    keys: tuple
    label: str
    kind: str                       # bool|int|float|combo|str
    default: object = None
    lo: float = 0.0
    hi: float = 0.0
    step: float = 1.0
    choices: list = field(default_factory=list)   # [(显示文本, yaml 值)]
    live: bool = False
    to_widget: Optional[Callable] = None          # yaml 值 → 控件值（缺省恒等）
    to_yaml: Optional[Callable] = None            # 控件值 → yaml 值（缺省恒等）


_PARAMS: list[Param] = [
    # ---- 界面（实时生效） ----
    Param("界面（实时生效）", ("ui_theme", "pet_window", "base_height"),
          "角色立绘显示高度（px，无上限）", "int", default=280, lo=160, hi=9999,
          live=True),  # 2026-09-12 取消原 560 上限（9999=防溢出的等效无上限，覆盖 8K）
    Param("界面（实时生效）", ("ui_theme", "pet_window", "opacity"),
          "立绘不透明度（0.1~1.0）", "float", default=1.0, lo=0.1, hi=1.0,
          step=0.05, live=True),
    Param("界面（实时生效）", ("ui_theme", "bubble", "scale"),
          "气泡整体缩放", "float", default=1.0, lo=0.7, hi=2.0, step=0.05, live=True),
    Param("界面（实时生效）", ("ui_theme", "bubble", "bg_opacity"),
          "气泡背景不透明度（文字恒全不透明）", "float", default=0.92,
          lo=0.05, hi=1.0, step=0.05, live=True),
    Param("界面（实时生效）", ("ui_theme", "bubble", "text_opacity"),
          "气泡文字不透明度", "float", default=1.0, lo=0.05, hi=1.0,
          step=0.05, live=True),
    Param("界面（实时生效）", ("ui_theme", "bubble", "font_size"),
          "气泡基础字号（缩放另乘）", "int", default=14, lo=8, hi=28, live=True),
    Param("界面（实时生效）", ("ui_theme", "bubble", "width"),
          "气泡固定宽度（0=按内容自适应）", "int", default=0, lo=0, hi=800, live=True),
    Param("界面（实时生效）", ("ui_theme", "bubble", "height"),
          "气泡固定高度（0=按内容自适应）", "int", default=0, lo=0, hi=600, live=True),
    Param("界面（实时生效）", ("ui_theme", "bubble", "max_sentences"),
          "气泡同时显示句数（1=单句）", "int", default=3, lo=1, hi=6, live=True),
    Param("界面（实时生效）", ("ui_theme", "input_bar", "width"),
          "输入栏宽度（px）", "int", default=320, lo=200, hi=560, live=True),
    Param("界面（实时生效）", ("ui_theme", "input_bar", "font_size"),
          "输入栏字号", "int", default=12, lo=9, hi=20, live=True),
    # ---- 音频 ----
    Param("音频（重启生效）", ("audio", "aec"), "回声消除 AEC", "combo",
          default="speex", choices=[("speex（Windows 缺省）", "speex"),
                                    ("webrtc（Linux/macOS）", "webrtc"),
                                    ("none（直通）", "none")]),
    Param("音频（重启生效）", ("audio", "barge_in"), "外放打断（barge-in）", "bool",
          default=False),
    Param("音频（重启生效）", ("voice", "vad", "tail_adaptive"), "尾点自适应（切早上浮）",
          "bool", default=True),
    Param("音频（重启生效）", ("voice", "vad", "tail_max_ms"), "自适应尾点上限（ms）",
          "int", default=1500, lo=700, hi=3000),
    # ---- 行为与分寸 ----
    Param("行为与分寸（重启生效）", ("behavior", "idle_sniff", "enabled"),
          "闲置嗅探（识屏主动关心）", "bool", default=False),
    Param("行为与分寸（重启生效）", ("behavior", "idle_sniff", "hours"),
          "闲置多久搭话一次（小时）", "float", default=4.0, lo=0.5, hi=24.0, step=0.5),
    Param("行为与分寸（重启生效）", ("behavior", "screenshot_hotkey"),
          "手动截屏热键", "str", default="Ctrl+Shift+M"),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "enabled"),
          "主动搭话总开关", "bool", default=True),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "cooldown_min_gap_s"),
          "任意两次主动搭话最小间隔（秒）", "int", default=600, lo=0, hi=7200),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "strength_threshold"),
          "搭话强度阈值", "float", default=0.4, lo=0.0, hi=1.0, step=0.05),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "greeting", "enabled"),
          "时段问候", "bool", default=True),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "greeting", "hour"),
          "问候时刻（点）", "int", default=9, lo=0, hi=23),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "greeting", "text"),
          "问候台词", "str", default="早上呀！新的一天也请多指教。"),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "memory_hook_hours"),
          "记忆钩子提及间隔（小时）", "float", default=8.0, lo=1.0, hi=72.0, step=0.5),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "open_topic_hours"),
          "未回扣话题追问间隔（小时）", "float", default=2.0, lo=0.5, hi=24.0, step=0.5),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "screenshot_cooldown_s"),
          "手动截屏信号冷却（秒）", "int", default=30, lo=0, hi=600),
    Param("行为与分寸（重启生效）", ("behavior", "proactive", "fullscreen_whitelist"),
          "全屏白名单（逗号分隔进程名）", "str", default=[],
          to_widget=_wl_decode, to_yaml=_wl_encode),
    Param("行为与分寸（重启生效）", ("screen_triggers", "companion", "enabled"),
          "陪玩模式总开关（名单内全屏/最大化时吐槽）", "bool", default=False),
    Param("行为与分寸（重启生效）", ("screen_triggers", "companion", "apps"),
          "陪玩名单（进程名:类型，逗号分隔；类型 game/video/web）", "str", default=[],
          to_widget=_wl_decode, to_yaml=_wl_encode),
    Param("行为与分寸（重启生效）", ("screen_triggers", "companion", "cooldown_min"),
          "陪玩搭话冷却（分钟）", "int", default=3, lo=1, hi=60),
    Param("行为与分寸（重启生效）", ("screen_triggers", "companion", "max_per_session"),
          "每次会话搭话上限", "int", default=12, lo=1, hi=50),
    # ---- 语音识别 ----
    Param("语音识别 ASR（重启生效）", ("providers", "asr"), "ASR 提供方", "combo",
          default="mock", choices=[("whisper（本地）", "whisper"), ("mock", "mock")]),
    Param("语音识别 ASR（重启生效）", ("asr", "model_size"), "whisper 模型档", "combo",
          default="small", choices=[("tiny（最快）", "tiny"), ("base", "base"),
                                    ("small（折中）", "small"), ("medium（最准）", "medium")]),
    Param("语音识别 ASR（重启生效）", ("asr", "language"), "识别语言", "combo",
          default=None, choices=[("自动检测", None), ("中文", "zh"),
                                 ("日本語", "ja"), ("English", "en")]),
    Param("语音识别 ASR（重启生效）", ("asr", "beam_size"), "beam_size（越小越快）",
          "int", default=5, lo=1, hi=8),
    Param("语音识别 ASR（重启生效）", ("asr", "device"), "计算设备", "combo",
          default="cpu", choices=[("cpu", "cpu"), ("cuda", "cuda")]),
    Param("语音识别 ASR（重启生效）", ("asr", "compute_type"), "量化类型", "combo",
          default="int8", choices=[("int8", "int8"), ("float32", "float32")]),
    # ---- Providers 与模型 ----
    Param("Providers 与模型（重启生效）", ("providers", "tts"), "TTS 提供方", "combo",
          default="mock",
          choices=[("gptsovits（自训音色）", "gptsovits"), ("mock", "mock"),
                   ("cloud_clone（云端克隆）", "cloud_clone")]),
    Param("Providers 与模型（重启生效）", ("providers", "vlm"), "VLM 提供方", "combo",
          default="mock", choices=[("openai（多模态）", "openai"), ("mock", "mock")]),
    Param("Providers 与模型（重启生效）", ("llm", "provider"), "对话 LLM 提供方", "combo",
          default="mock", choices=[("api（OpenAI 兼容）", "api"),
                                   ("mock", "mock")]),
    Param("Providers 与模型（重启生效）", ("llm", "api", "model"), "模型名", "str",
          default="deepseek-chat"),
    Param("Providers 与模型（重启生效）", ("llm", "api", "base_url"), "API 端点", "str",
          default="https://api.deepseek.com/v1"),
    Param("Providers 与模型（重启生效）", ("llm", "api", "temperature"),
          "温度", "float", default=0.8, lo=0.0, hi=2.0, step=0.05),
    Param("Providers 与模型（重启生效）", ("llm", "api", "timeout_s"),
          "API 超时（秒）", "int", default=60, lo=5, hi=600),
    Param("Providers 与模型（重启生效）", ("llm", "api", "price_cny_per_1m"),
          "每 1M token 价格（元，0=不计）", "float", default=0.0, lo=0.0, hi=1000.0,
          step=0.5, to_widget=lambda v: 0.0 if not v else float(v),
          to_yaml=lambda v: None if not v else float(v)),
    # ---- 视觉与摄像头 ----
    Param("视觉与摄像头（重启生效）", ("camera", "interval_s"),
          "摄像头采样间隔（秒，下限 15）", "int", default=45, lo=15, hi=600),
    Param("视觉与摄像头（重启生效）", ("camera", "max_side"),
          "摄像头帧最长边（px）", "int", default=1024, lo=480, hi=2048),
    # ---- 隐私 ----
    Param("隐私（实时生效）", ("camera", "enabled"),
          "摄像头感知（首次开启弹同意框；画面定期发往 VLM，DND 时停发）",
          "bool", default=False, live=True),
    Param("隐私（实时生效）", ("voice", "autostart_mic"),
          "麦克风开机自启（启动恢复上次 🎤 开关状态）", "bool", default=False,
          live=True),
    Param("视觉与摄像头（重启生效）", ("vision", "observe_prompt"),
          "摄像头观察提示词", "str", default=""),
    Param("视觉与摄像头（重启生效）", ("vision", "cooldown_min"),
          "摄像头观察冷却（分钟）", "int", default=10, lo=1, hi=240),
    Param("视觉与摄像头（重启生效）", ("vision", "max_per_day"),
          "摄像头观察每日上限", "int", default=5, lo=1, hi=100),
    # ---- 服务 ----
    Param("服务（重启生效）", ("server", "port"), "服务端口（仅本地回环）", "int",
          default=9800, lo=1024, hi=65535),
    # ---- 工具模式（M-TOOL；托盘开关为实时生效，此处改动重启生效）----
    Param("工具模式（重启生效）", ("tools", "enabled"),
          "工具模式总闸（false=全部提醒关闭）", "bool", default=True),
    Param("工具模式（重启生效）", ("tools", "pomodoro", "work_min"),
          "番茄钟专注时长（分钟）", "int", default=25, lo=1, hi=120),
    Param("工具模式（重启生效）", ("tools", "pomodoro", "break_min"),
          "番茄钟休息时长（分钟）", "int", default=5, lo=1, hi=60),
    Param("工具模式（重启生效）", ("tools", "water_min"),
          "喝水提醒间隔（分钟，0=关）", "int", default=45, lo=0, hi=360),
    Param("工具模式（重启生效）", ("tools", "sedentary_min"),
          "久坐提醒间隔（分钟，0=关）", "int", default=90, lo=0, hi=480),
    Param("工具模式（重启生效）", ("tools", "active_idle_s"),
          "离开输入多少秒后暂停健康提醒", "int", default=300, lo=0, hi=3600),
    Param("工具模式（重启生效）", ("tools", "hourly", "enabled"),
          "整点报时", "bool", default=False),
    Param("工具模式（重启生效）", ("tools", "hourly", "quiet_start"),
          "报时免打扰起始时（24h 制）", "int", default=22, lo=0, hi=23),
    Param("工具模式（重启生效）", ("tools", "hourly", "quiet_end"),
          "报时免打扰结束时", "int", default=8, lo=0, hi=23),
    Param("工具模式（重启生效）", ("tools", "festivals", "enabled"),
          "节日/纪念日问候", "bool", default=True),
    Param("工具模式（重启生效）", ("tools", "festivals", "custom"),
          "自定义纪念日（\"05-20 纪念日, 12-01 生日\"）", "str", default=""),
    Param("工具模式（重启生效）", ("tools", "festivals", "hour"),
          "节日问候触发时刻（24h 制）", "int", default=9, lo=0, hi=23),
]

_PARAM_GROUPS = [g for g in dict.fromkeys(p.group for p in _PARAMS)]


# ---------------------------------------------------------------- 探活线程


class _ProbeThread(QThread):
    """Providers 探活（后台线程；asyncio.run 包链的异步探活）。"""

    results_ready = Signal(list)

    def run(self) -> None:
        import asyncio

        try:
            self.results_ready.emit(asyncio.run(self._probe_all()))
        except Exception as exc:
            logger.warning("Providers 探活线程异常", exc_info=True)
            self.results_ready.emit([("Providers", False,
                                      f"探活异常：{exc.__class__.__name__}")])

    @staticmethod
    async def _probe_all() -> list[tuple[str, bool, str]]:
        from server.providers.factory import build_asr, build_tts_chain

        out: list[tuple[str, bool, str]] = []
        chain = build_tts_chain()
        await chain.lock()                       # 链内逐级探活，锁定实际可用级
        names = chain.level_names()
        active_name = chain.active_name or names[-1]
        idx = names.index(active_name) if active_name in names else len(names) - 1
        for i, name in enumerate(names):
            out.append((f"TTS·{name}", i >= idx,
                        "可用（锁定级）" if i == idx else "不可用/被跳过"))
        asr = build_asr()
        out.append(("ASR", True, f"提供方：{getattr(asr, 'name', 'mock')}"))
        return out


def _page_holder(layout) -> QWidget:
    """QVBoxLayout → 页容器（QTabWidget 只收 widget）。"""
    holder = QWidget()
    holder.setLayout(layout)
    return holder


class _MetricsThread(QThread):
    """Token 监控轮询（后台线程；每 interval 拉一次 /metrics，GUI 线程只收信号）。

    ``fetch`` 供测试注入（生产 None → httpx 直连 127.0.0.1:port/metrics）；
    stop() 后最迟 interval 内退出（sleep 按 0.2s 切片查标志）。
    """

    metrics_ready = Signal(object)          # {"ok": dict|None, "error": str}

    def __init__(self, port: int, *, fetch=None, interval_s: float = 2.0) -> None:
        super().__init__()
        self._port = int(port)
        self._fetch = fetch
        self._interval_s = float(interval_s)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        while not self._stop:
            payload: dict = {"ok": None, "error": ""}
            try:
                if self._fetch is not None:
                    payload["ok"] = self._fetch()
                else:
                    import httpx

                    resp = httpx.get(
                        f"http://127.0.0.1:{self._port}/metrics", timeout=1.5)
                    payload["ok"] = resp.json()
            except Exception as exc:
                payload["error"] = f"{exc.__class__.__name__}: {exc}"
            if self._stop:
                break
            self.metrics_ready.emit(payload)
            waited = 0.0
            while waited < self._interval_s and not self._stop:
                time.sleep(0.2)
                waited += 0.2


class SettingsWindow(QDialog):
    """设置窗（四页）。live_ui：keys 元组 → 实时应用回调（界面组用，run_client 注入）。"""

    def __init__(self, *, tray=None, parent=None,
                 settings_path: Path | None = None,
                 live_ui: Optional[dict[tuple, Callable]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("miyako 设置")
        self.resize(560, 520)
        self._tray = tray
        self._settings_path = settings_path
        self._live_ui = live_ui or {}
        self._probe_thread: _ProbeThread | None = None
        self._token_thread: _MetricsThread | None = None
        self._token_port = 9800
        self._admin_obj = None                   # MemoryAdmin 懒构造（首用才开库）
        self._param_rows: list[tuple[Param, Callable, object]] = []
        self._param_widgets: dict[tuple, object] = {}   # keys → 控件本体（测试/联动）

        tabs = QTabWidget(self)
        tabs.addTab(self._build_params_tab(), "参数调整")
        tabs.addTab(self._build_probe_tab(), "Providers")
        tabs.addTab(self._build_relationship_tab(), "关系")
        tabs.addTab(self._build_memory_tab(), "记忆")
        tabs.addTab(self._build_token_tab(), "Token 监控")
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)

        apply_btn = QPushButton("应用")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)

        # 轮询线程只在 hideEvent 停——窗口开着时托盘「完全退出」不走 hideEvent，
        # 运行中的 QThread 在退出期被 GC = qFatal 中止，补 aboutToQuit 兜底
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_token_poll)

    # ---------------- 参数调整页（声明式生成） ----------------
    def _build_params_tab(self) -> QWidget:
        page = QVBoxLayout()
        hint = QLabel("「实时」项改动立即生效；其余项应用时统一提醒重启。")
        hint.setWordWrap(True)
        page.addWidget(hint)
        for group in _PARAM_GROUPS:
            form = QFormLayout()
            if group.startswith("行为与分寸"):
                blurb = QLabel(_IDLE_SNIFF_BLURB)
                blurb.setWordWrap(True)
                form.addRow(blurb)
            if self._tray is not None and group.startswith("行为与分寸"):
                self._dnd_box = QCheckBox("勿扰模式（DND，实时）——关闭一切主动搭话")
                self._dnd_box.setChecked(self._tray.dnd)
                form.addRow("勿扰", self._dnd_box)
            for p in _PARAMS:
                if p.group != group:
                    continue
                widget, getter = self._make_param_widget(p)
                self._param_rows.append((p, getter, widget))
                self._param_widgets[p.keys] = widget
                form.addRow(p.label, widget)
            box = QWidget()
            box.setLayout(form)
            page.addWidget(box)
        page.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(_page_holder(page))
        return scroll

    def _make_param_widget(self, p: Param) -> tuple[QWidget, Callable]:
        cur = _read_yaml_path(self._settings_path, p.keys, p.default)
        if p.to_widget is not None:
            cur = p.to_widget(cur)
        if p.kind == "bool":
            widget = QCheckBox("开启")
            widget.setChecked(bool(cur))

            def getter(w=widget):
                return w.isChecked()
        elif p.kind == "int":
            widget = QSpinBox()
            widget.setRange(int(p.lo), int(p.hi))
            widget.setValue(int(float(cur)))
            getter = widget.value
        elif p.kind == "float":
            widget = QDoubleSpinBox()
            widget.setRange(float(p.lo), float(p.hi))
            widget.setSingleStep(float(p.step) if p.step else 0.1)
            widget.setDecimals(2)
            widget.setValue(float(cur))
            getter = widget.value
        elif p.kind == "combo":
            widget = QComboBox()
            for text, data in p.choices:
                widget.addItem(text, data)
            idx = next((i for i, (_, data) in enumerate(p.choices)
                        if data == cur), 0)
            widget.setCurrentIndex(idx)
            getter = widget.currentData
        else:                                        # str
            widget = QLineEdit(str(cur) if cur is not None else "")
            getter = widget.text
        if p.live:
            def _on_change(*_a, p=p, getter=getter):
                val = getter()
                fn = self._live_ui.get(p.keys)
                if fn is not None:
                    try:
                        fn(val)
                    except Exception:
                        logger.warning("实时应用 %s 失败", p.keys, exc_info=True)
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.valueChanged.connect(_on_change)
            elif isinstance(widget, QLineEdit):
                widget.editingFinished.connect(_on_change)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(_on_change)      # 隐私组 bool 实时开关
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(_on_change)
        return widget, getter

    # ---------------- Providers 探活页 ----------------
    def _build_probe_tab(self) -> QWidget:
        page = QVBoxLayout()
        self._probe_lights = QVBoxLayout()
        hint = QLabel("TTS 四级链逐级探活 + ASR 提供方（离线项自动降级）。")
        hint.setWordWrap(True)
        page.addWidget(hint)
        page.addLayout(self._probe_lights)
        self._probe_btn = QPushButton("开始探活")
        self._probe_btn.clicked.connect(self._run_probe)
        page.addWidget(self._probe_btn)
        page.addStretch(1)
        return _page_holder(page)

    def _run_probe(self) -> None:
        self._probe_btn.setEnabled(False)
        self._probe_thread = _ProbeThread()
        self._probe_thread.results_ready.connect(self._on_probe_done)
        self._probe_thread.start()

    def _on_probe_done(self, results: list) -> None:
        self._probe_btn.setEnabled(True)
        while self._probe_lights.count():
            item = self._probe_lights.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for name, ok, detail in results:
            lamp = QLabel(f"{'🟢' if ok else '🔴'} {name} —— {detail}")
            lamp.setToolTip("状态灯：🟢 可用 / 🔴 不可用或被降级跳过")
            self._probe_lights.addWidget(lamp)

    # ---------------- 关系页（只读 + 一键重置） ----------------
    def _build_relationship_tab(self) -> QWidget:
        page = QVBoxLayout()
        self._rel_label = QLabel("读取中…")
        self._rel_label.setWordWrap(True)
        page.addWidget(self._rel_label)
        page.addStretch(1)
        reset_btn = QPushButton("一键重置记忆与关系…")
        reset_btn.clicked.connect(self._confirm_reset)
        page.addWidget(reset_btn)
        return _page_holder(page)

    def _refresh_relationship(self) -> None:
        try:
            rel = self._memory_admin().read_relationship()
        except Exception:
            logger.warning("关系数值读取失败", exc_info=True)
            rel = None
        if rel is None:
            self._rel_label.setText("尚未建立关系数据（互动后会自动生长）。")
        else:
            self._rel_label.setText(
                f"好感 {rel['affection']:.1f} / 100　信任 {rel['trust']:.1f} / 100"
                "\n（数值随互动自动演化，此处只读）")

    def _confirm_reset(self) -> None:
        answer = QMessageBox.question(
            self, "重置记忆与关系",
            "将清空全部记忆、话题钩子与关系数值，且不可恢复。\n"
            "（用于大幅调教人设后清除旧人格锚定）\n\n确定重置吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            counts = self._memory_admin().reset_all()
        except Exception:
            logger.warning("记忆/关系重置失败", exc_info=True)
            QMessageBox.warning(self, "重置失败", "重置未完成，详情见日志。")
            return
        self._refresh_relationship()
        self._refresh_memory_table()
        QMessageBox.information(
            self, "已重置",
            f"已清空：记忆 {counts['entries']} 条、钩子 {counts['hooks']} 条、"
            f"关系数值 {counts['dimensions']} 行。")

    # ---------------- 记忆管理页 ----------------
    def _build_memory_tab(self) -> QWidget:
        page = QVBoxLayout()
        self._memory_table = QTableWidget(0, 4)
        self._memory_table.setHorizontalHeaderLabels(
            ["id", "类型", "内容（截断）", "时间"])
        self._memory_table.horizontalHeader().setStretchLastSection(True)
        self._memory_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._memory_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        page.addWidget(self._memory_table)
        row = QHBoxLayout()
        view_btn = QPushButton("查看全文")
        view_btn.clicked.connect(self._view_entry)
        edit_btn = QPushButton("编辑（仅 fact）")
        edit_btn.clicked.connect(self._edit_fact)
        del_btn = QPushButton("删除单条")
        del_btn.clicked.connect(self._delete_entry)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh_memory_table)
        for btn in (view_btn, edit_btn, del_btn, refresh_btn):
            row.addWidget(btn)
        page.addLayout(row)
        return _page_holder(page)

    def _refresh_memory_table(self) -> None:
        try:
            rows = self._memory_admin().list_entries()
        except Exception:
            logger.warning("记忆列表读取失败", exc_info=True)
            rows = []
        self._memory_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            content = str(row["content"])
            short = content if len(content) <= 40 else content[:40] + "…"
            for col, value in enumerate((str(row["id"]), str(row["kind"]),
                                         short, str(row["ts"]))):
                self._memory_table.setItem(i, col, QTableWidgetItem(value))

    def _selected_entry(self) -> tuple[Optional[int], str]:
        sel = self._memory_table.selectionModel().selectedRows()
        if not sel:
            return None, ""
        row = sel[0].row()
        entry_id = int(self._memory_table.item(row, 0).text())
        kind = self._memory_table.item(row, 1).text()
        return entry_id, kind

    def _view_entry(self) -> None:
        entry_id, _kind = self._selected_entry()
        if entry_id is None:
            return
        row = self._memory_admin().get_entry(entry_id)
        if row is None:
            return
        QMessageBox.information(self, f"记忆 #{entry_id}（{row['kind']}）",
                                f"{row['content']}\n\n—— {row['ts']}，"
                                f"重要度 {row['importance']}")

    def _edit_fact(self) -> None:
        entry_id, kind = self._selected_entry()
        if entry_id is None:
            return
        if kind != "fact":
            QMessageBox.information(self, "编辑", "仅 fact 类型支持编辑"
                                    "（diary 由模型生成，只读/删除）。")
            return
        row = self._memory_admin().get_entry(entry_id)
        if row is None:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle(f"编辑 fact #{entry_id}")
        dialog.setLabelText("校对记忆内容（保存即生效于下轮检索）：")
        layout = dialog.layout()
        editor = QPlainTextEdit(str(row["content"]))
        editor.resize(420, 140)
        layout.addWidget(editor)
        dialog.setSizeGripEnabled(True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_text = editor.toPlainText().strip()
        if not new_text:
            QMessageBox.warning(self, "编辑", "内容为空，未保存。")
            return
        if self._memory_admin().edit_entry(entry_id, new_text):
            self._refresh_memory_table()

    def _delete_entry(self) -> None:
        entry_id, kind = self._selected_entry()
        if entry_id is None:
            return
        answer = QMessageBox.question(
            self, "删除记忆", f"删除 {kind} #{entry_id}？删除后下轮检索不到。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._memory_admin().delete_entry(entry_id):
            self._refresh_memory_table()

    # ---------------- 公共 ----------------
    def _memory_admin(self):
        """MemoryAdmin 懒构造（首用才开角色库；激活包判定与服务端同源）。"""
        if self._admin_obj is None:
            from server.memory.admin import MemoryAdmin
            from server.memory.store import MemoryStore
            from server.memory.vector import MemoryRetriever

            store = MemoryStore()
            self._admin_obj = MemoryAdmin(store, MemoryRetriever(store))
        return self._admin_obj

    def _set_param_widget(self, p: Param, widget, value) -> None:
        """按控件类型回填文件现值（刷新用；类型与 _make_param_widget 一致）。"""
        if p.kind == "bool":
            widget.setChecked(bool(value))
        elif p.kind == "int":
            widget.setValue(int(float(value)))
        elif p.kind == "float":
            widget.setValue(float(value))
        elif p.kind == "combo":
            idx = next((i for i, (_, data) in enumerate(p.choices)
                        if data == value), 0)
            widget.setCurrentIndex(idx)
        else:                                        # str
            widget.setText(str(value) if value is not None else "")

    def _refresh_params_from_file(self) -> None:
        """全部参数控件 ← settings.yaml 现值（打开/显示窗口时调用）。

        防「过期覆盖」（2026-09-18 asr 两次被回退 mock 的根治）：设置窗若长开，
        打开时读入的内存值会变旧——期间 settings.yaml 被外部修改（另一次会话/
        手工编辑/升级），_apply 会按控件旧值写回=外部修改被静默回滚。显示即
        全量同步一次，把过期窗口压缩到「窗口可见期间」（可见期间的外部修改
        仍可能在保存时被覆盖——保存前逐键比对留待后续）。
        """
        for p, _getter, widget in self._param_rows:
            cur = _read_yaml_path(self._settings_path, p.keys, p.default)
            if p.to_widget is not None:
                cur = p.to_widget(cur)
            try:
                self._set_param_widget(p, widget, cur)
            except Exception:
                logger.debug("参数控件刷新失败 %s", p.keys, exc_info=True)
        if getattr(self, "_dnd_box", None) is not None and self._tray is not None:
            self._dnd_box.setChecked(self._tray.dnd)

    def _apply(self) -> None:
        if self._tray is not None and getattr(self, "_dnd_box", None) is not None:
            self._tray._act_dnd.setChecked(self._dnd_box.isChecked())   # 联动托盘 DND
        restart_changed: list[str] = []
        for p, getter, _widget in self._param_rows:
            val_widget = getter()
            # 比较在控件域进行（to_yaml 是单向映射，对称套用会误判——
            # 如 _wl_encode(str([])) == ["[]"]，2026-09-11 实测踩坑）
            cur = _read_yaml_path(self._settings_path, p.keys, p.default)
            cur_widget = p.to_widget(cur) if p.to_widget is not None else cur
            try:
                unchanged = (val_widget == cur_widget)
            except Exception:
                unchanged = False
            if unchanged:
                continue
            val_yaml = p.to_yaml(val_widget) if p.to_yaml is not None else val_widget
            _rewrite_key_at_path(self._settings_path, p.keys, _yaml_scalar(val_yaml))
            if not p.live:
                restart_changed.append(p.label)
            logger.info("参数已写入 %s（%s）", ".".join(p.keys), val_yaml)
        if restart_changed:
            self._confirm_restart(restart_changed)
        self.accept()

    def _confirm_restart(self, labels: list[str]) -> None:
        """🔑 重启生效参数改动 → 统一提醒并可立即重启（复用切换角色的重启助手）。"""
        body = "以下参数需要重启 miyako 才能生效：\n　· " + "\n　· ".join(labels)
        reply = QMessageBox.question(
            self, "需要重启", body + "\n\n现在立即重启 miyako 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            from client.switcher import schedule_restart

            schedule_restart()
            if self._tray is not None:
                self._tray.quit_all()                # 等服务端 pid 消失后自动重拉
        else:
            QMessageBox.information(self, "已保存",
                                    "已写入配置，下次重启 miyako 后生效。")

    def showEvent(self, event) -> None:              # 打开即刷新只读数据
        super().showEvent(event)
        self._refresh_params_from_file()             # 参数控件 ← 文件现值（防过期覆盖）
        self._refresh_relationship()
        self._refresh_memory_table()
        self._start_token_poll()

    def hideEvent(self, event) -> None:              # 隐藏/关闭即停轮询（省资源）
        self._stop_token_poll()
        super().hideEvent(event)

    # ---------------- Token 监控页（2026-09-12 用户需求③） ----------------

    def _build_token_tab(self) -> QWidget:
        layout = QVBoxLayout()
        self._token_status = QLabel("尚未刷新（打开本窗口后每 2 秒拉取 server /metrics）")
        self._token_status.setWordWrap(True)
        layout.addWidget(self._token_status)

        # 会话级统计（2026-09-17 用户需求）：本次使用总/单轮最高/最低 + 陪玩独立视角
        self._token_session = QLabel("本次使用：—")
        self._token_session.setWordWrap(True)
        layout.addWidget(self._token_session)

        layout.addWidget(QLabel("本次启动（server 进程内）——分角色："))
        self._token_table = QTableWidget(0, 7)
        self._token_table.setHorizontalHeaderLabels(
            ["角色", "请求", "prompt", "completion", "缓存命中 tok",
             "缓存命中率", "计量"])
        self._token_table.horizontalHeader().setStretchLastSection(True)
        self._token_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._token_table)

        layout.addWidget(QLabel("跨重启累计（随角色库存档 data/memory/{角色}.usage.json）："))
        self._token_lifetime_table = QTableWidget(0, 6)
        self._token_lifetime_table.setHorizontalHeaderLabels(
            ["角色", "请求", "prompt", "completion", "缓存命中 tok", "估算请求"])
        self._token_lifetime_table.horizontalHeader().setStretchLastSection(True)
        self._token_lifetime_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._token_lifetime_table)

        self._token_cost = QLabel("成本：—")
        self._token_cost.setWordWrap(True)
        layout.addWidget(self._token_cost)
        layout.addStretch(1)
        return _page_holder(layout)

    def _start_token_poll(self) -> None:
        if self._token_thread is not None:
            return
        try:
            self._token_port = int(_read_yaml_path(
                self._settings_path, ("server", "port"), 9800) or 9800)
        except (TypeError, ValueError):
            self._token_port = 9800
        thread = _MetricsThread(self._token_port)
        thread.metrics_ready.connect(self._apply_token_metrics)
        thread.start()
        self._token_thread = thread

    def _stop_token_poll(self) -> None:
        if self._token_thread is not None:
            self._token_thread.stop()
            self._token_thread.wait(3000)
            self._token_thread = None

    def _apply_token_metrics(self, payload: dict) -> None:
        """轮询结果 → 表格（测试可直接注入 payload 调用，不经线程）。"""
        data, error = payload.get("ok"), payload.get("error") or ""
        if error or not isinstance(data, dict):
            self._token_status.setText(
                f"⚠️ 无法读取 server /metrics（端口 {self._token_port}）："
                f"{error or '空响应'}——server 未启动？")
            return
        self._token_status.setText(
            f"已刷新 {time.strftime('%H:%M:%S')} · 数据源 "
            f"127.0.0.1:{self._token_port}/metrics")
        self._apply_session_tokens(data.get("tokens_session") or {})
        tokens = data.get("tokens") or {}
        self._fill_token_table(
            self._token_table, (tokens.get("per_role") or {}).items(),
            process_mode=True)
        lifetime_roles = (data.get("tokens_lifetime") or {}).get("roles") or {}
        self._fill_token_table(self._token_lifetime_table,
                               lifetime_roles.items(), process_mode=False)
        cost = data.get("cost") or {}
        price = cost.get("price_cny_per_1m")
        price_cached = cost.get("price_cny_per_1m_cached")
        if price:
            price_txt = f"¥{price}/1M tok"
            if price_cached is not None:
                price_txt += f"（缓存命中 ¥{price_cached}/1M）"
        else:
            price_txt = "未配置价格（llm.api.price_cny_per_1m，不计成本）"
        self._token_cost.setText(
            f"成本：{cost.get('replies', 0)} 轮对话 · 合计 "
            f"{cost.get('total_tokens', 0)} tok（其中缓存命中 "
            f"{cost.get('cached_tokens', 0)}）· ¥{cost.get('total_cny', 0.0)}"
            f"（{price_txt}）· 单轮 ¥{cost.get('avg_per_reply_cny', 0.0)}")

    @staticmethod
    def _format_session_tokens(session: dict) -> str:
        """tokens_session → 「本次使用 + VLM + 陪玩独立视角」多行文本。"""
        if not session:
            return "本次使用：—（server 未提供会话统计）"
        min_turn = session.get("min_turn_tokens")
        min_txt = "—" if min_turn is None else str(min_turn)
        text = (f"本次使用（自 {session.get('started_at', '—')}）："
                f"总 token {session.get('total_tokens', 0)} ｜ "
                f"{session.get('turns', 0)} 轮 ｜ 单轮最高 "
                f"{session.get('max_turn_tokens', 0)} / 最低 {min_txt}")
        vlm = session.get("vlm_tokens") or 0
        if vlm:
            text += f"\n画面理解（VLM）：{vlm} tok"
        companion = (session.get("sources") or {}).get("companion")
        if companion:
            c_min = companion.get("min_turn_tokens")
            c_min_txt = "—" if c_min is None else str(c_min)
            text += (f"\n陪玩模式：总 token {companion.get('total_tokens', 0)}"
                     f"（{companion.get('turns', 0)} 轮，单轮最高 "
                     f"{companion.get('max_turn_tokens', 0)} / 最低 {c_min_txt}")
            c_vlm = companion.get("vlm_tokens") or 0
            if c_vlm:
                text += f"；VLM {c_vlm} tok"
            text += "）"
        else:
            text += "\n陪玩模式：本次会话暂无陪玩消耗"
        return text

    def _apply_session_tokens(self, session: dict) -> None:
        self._token_session.setText(self._format_session_tokens(session))

    @staticmethod
    def _fill_token_table(table: QTableWidget, entries, *, process_mode: bool) -> None:
        rows = sorted(entries)
        table.setRowCount(len(rows))
        for r, (role, entry) in enumerate(rows):
            entry = entry or {}
            prompt = entry.get("prompt_tokens", 0)
            hit = entry.get("cached_tokens", 0)
            values = [role, entry.get("requests", 0), prompt,
                      entry.get("completion_tokens", 0), hit]
            if process_mode:
                reported = entry.get("cache_reported_requests", 0)
                rate = (f"{hit / prompt * 100:.0f}%"
                        if reported and prompt else "—")
                estimated = entry.get("estimated_requests", 0)
                if estimated and estimated == entry.get("requests"):
                    mode = "估算"
                elif estimated:
                    mode = "真实+估算"
                else:
                    mode = "真实"
                values += [rate, mode]
            else:
                values.append(entry.get("estimated_requests", 0))
            for c, value in enumerate(values):
                table.setItem(r, c, QTableWidgetItem(str(value)))
