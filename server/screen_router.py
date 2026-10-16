"""识屏双路路由（M+3.3）+ 规则引擎（M+3.4）—— ScreenShot → 话题钩子 → gate → 搭话。

路由顺序（plan 定版）：
  ①hash 对比无实质变化 → 丢弃（与服务端最近一帧比对，防重复上行重复处理）；
  ②OCR 文本过规则（M+3.4，screen_triggers.rules 配置驱动）：
      命中且文本充分（置信度均值>0.8 且有效字符>50）→ **纯文本进话题，VLM 不调用**；
      命中但需语境（image 路由 / 文本不充分）→ 图像送 VLM；
      未命中 → 静默丢弃（手动热键则回落 M+2.3 的通用截图信号）；
  ③VLM 超时 2s / 失败 → 跳过屏幕上下文（识屏静默忽略）；
  ④结果 → 话题 → behavior 全量 gate（DND→白名单→冷却→强度→LLM 时机）→ 搭话。

🔑 隐私三道闸之服务端半边：嗅探生成内容**全内存态用完即弃，绝不写
memory.db**（本模块零 store 依赖，零文件写）；识屏搭话有**独立 4h 冷却**
（cooldown_hours，screen_triggers 节），行为侧全量 gate 照过不误。

priority 静态手编（high > mid > low），❌ 不做规则自动调优（§4.3 否决清单）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Callable, Optional

from server.protocol import ScreenShot

logger = logging.getLogger(__name__)

TEXT_MIN_MEAN_CONF = 0.8     # 文本充分判据一：置信度均值（plan 定版）
TEXT_MIN_CHARS = 50          # 文本充分判据二：有效字符数（plan 定版）
VLM_TIMEOUT_S = 2.0          # ③ VLM 超时（plan 定版；超时→跳过屏幕上下文）
DEFAULT_COOLDOWN_H = 4.0     # 识屏搭话独立冷却（plan 定版 4h）
_SIGNIFICANT_RATIO = 0.125   # hash「无实质变化」判据：位距 < 总位数 12.5% 视为
                             # 同屏（与 client/screen_capture 比例同规）

_PRIORITY_RANK = {"high": 0, "mid": 1, "low": 2}

DEFAULT_SCREEN_PROMPT = ("用不超过40字描述屏幕上值得注意的内容"
                         "（哪个应用/发生了什么）；没有值得注意的只输出 SKIP。")

DEFAULT_COMPANION_PROMPT = ("你是和用户一起看这个画面的陪玩伙伴。用不超过40字"
                            "对画面说一句自然的吐槽或感想；没有值得说的只输出 SKIP。")
COMPANION_COOLDOWN_MIN = 3.0  # 陪玩搭话独立冷却（客户端已节流，此处防御性二次限速）


def parse_cooldown(value) -> Optional[float]:
    """规则 cooldown 解析："8h"/"30m"/"45s" 或裸数字（=小时）。非法 → None。"""
    try:
        if isinstance(value, (int, float)):
            return float(value) * 3600.0
        text = str(value).strip().lower()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hms]?)", text)
        if m is None:
            return None
        amount = float(m.group(1))
        unit = m.group(2) or "h"
        return amount * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit]
    except (TypeError, ValueError):
        return None


class ScreenRule:
    """单条识屏规则（name/pattern/route/priority/cooldown）。"""

    def __init__(self, name: str, pattern: str, route: str,
                 priority: str = "low", cooldown_s: float = 0.0) -> None:
        self.name = name
        self.regex = re.compile(pattern)
        self.route = route
        self.priority = priority
        self.rank = _PRIORITY_RANK.get(priority, _PRIORITY_RANK["low"])
        self.cooldown_s = max(0.0, cooldown_s)
        self.last_fired = -1e18

    def cooling(self, mono: float) -> bool:
        return mono - self.last_fired < self.cooldown_s


class ScreenRules:
    """规则引擎（M+3.4）：加载（坏规则跳过+warning）与匹配（优先序，冷却跳过）。"""

    def __init__(self, settings: Optional[dict] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.rules: list[ScreenRule] = []
        raw = ((settings or {}).get("screen_triggers") or {}).get("rules") or []
        if not isinstance(raw, list):
            logger.warning("screen_triggers.rules 非列表，忽略（fail-open：无规则=识屏静默）")
            return
        for i, item in enumerate(raw):
            rule = self._build(i, item)
            if rule is not None:
                self.rules.append(rule)
        logger.info("识屏规则已加载 %d 条（共 %d 项）", len(self.rules), len(raw))

    @staticmethod
    def _build(index: int, item) -> Optional[ScreenRule]:
        try:
            if not isinstance(item, dict):
                raise ValueError("规则项不是映射")
            name = str(item.get("name") or f"rule_{index}")
            pattern = item.get("pattern")
            if not pattern:
                raise ValueError("缺少 pattern")
            route = str(item.get("route") or "text")
            if route not in ("text", "image"):
                raise ValueError(f"route 非法：{route!r}")
            cooldown_s = parse_cooldown(item.get("cooldown")) or 0.0
            return ScreenRule(name, str(pattern), route,
                              priority=str(item.get("priority") or "low"),
                              cooldown_s=cooldown_s)
        except (ValueError, TypeError, re.error):
            logger.warning("识屏规则 #%d 非法已跳过（fail-open）：%r",
                           index, item, exc_info=True)
            return None

    def match(self, text: str) -> Optional[ScreenRule]:
        """按优先序找首条命中且不在冷却的规则；无 → None。"""
        mono = self._clock()
        for rule in sorted(self.rules, key=lambda r: r.rank):
            if rule.cooling(mono):
                continue
            try:
                if rule.regex.search(text):
                    return rule
            except Exception:
                logger.debug("规则 %s 匹配异常", rule.name, exc_info=True)
        return None

    def note_fired(self, rule: ScreenRule) -> None:
        rule.last_fired = self._clock()


def _hash_changed(hex_a: Optional[str], hex_b: Optional[str]) -> bool:
    if not hex_a or not hex_b:
        return True
    try:
        dist = (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()
    except (ValueError, TypeError):
        return True
    return dist >= max(1, int(min(len(hex_a), len(hex_b)) * 4 * _SIGNIFICANT_RATIO))


def _valid_chars(text: str) -> int:
    """有效字符数（字母/数字/CJK 等文种字符；空白与标点不计）。"""
    return sum(1 for ch in text if ch.isalnum())


def _text_sufficient(ocr_lines) -> bool:
    """「文本充分」：置信度均值 > 0.8 且有效字符 > 50（plan 定版双判据）。"""
    if not ocr_lines:
        return False
    mean_conf = sum(line.conf for line in ocr_lines) / len(ocr_lines)
    text = "".join(line.text for line in ocr_lines)
    return mean_conf > TEXT_MIN_MEAN_CONF and _valid_chars(text) > TEXT_MIN_CHARS


class ScreenRouter:
    """双路路由。behavior 注入 orchestrator.behavior（request_screen_speak，M+3.3）。"""

    def __init__(self, *, orchestrator, vlm, behavior,
                 rules: Optional[ScreenRules] = None,
                 settings: Optional[dict] = None,
                 clock: Callable[[], float] = time.monotonic,
                 notify: Optional[Callable[[str], None]] = None) -> None:
        cfg = ((settings or {}).get("screen_triggers") or {})
        self.observe_prompt = str(cfg.get("observe_prompt") or DEFAULT_SCREEN_PROMPT)
        self.cooldown_s = max(0.0, float(cfg.get("cooldown_hours", DEFAULT_COOLDOWN_H)) * 3600.0)
        comp = (cfg.get("companion") or {})
        self._companion_cooldown_s = max(
            0.0, float(comp.get("cooldown_min", COMPANION_COOLDOWN_MIN)) * 60.0)
        self._companion_prompt = str(comp.get("prompt") or DEFAULT_COMPANION_PROMPT)
        self._last_companion = -1e18
        self.orchestrator = orchestrator
        self.vlm = vlm
        self.behavior = behavior
        self.rules = rules or ScreenRules(settings)
        self._clock = clock
        self._notify = notify or (lambda msg: logger.info("%s", msg))
        self._last_hash: Optional[str] = None
        self._last_submit = -1e18
        self._inflight = False

    async def handle(self, msg: ScreenShot) -> Optional[str]:
        """处理一帧识屏；返回进入话题的上下文文本（丢弃路径返回 None）。绝不抛异常。"""
        if self._inflight or self.orchestrator.is_busy:
            return None                              # 上一帧处理中 / reply 忙 → 丢弃
        # 并发帧闸（陪玩帧 ~5s 一发，VLM 一趟可超 2s——重入会产生重复 VLM 调用
        # 与双计冷却；此前 _inflight 只初始化从未置位，闸是死的）
        self._inflight = True
        try:
            return await self._handle_inner(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("识屏路由异常（本帧丢弃）", exc_info=True)
            return None
        finally:
            self._inflight = False

    async def _handle_inner(self, msg: ScreenShot) -> Optional[str]:
        if msg.source == "companion":
            return await self._handle_companion(msg)  # 陪玩分支（M-PLAY）：独立冷却/提示词
        mono = self._clock()
        if mono - self._last_submit < self.cooldown_s:
            logger.debug("识屏独立冷却中（%.0fh），丢弃", self.cooldown_s / 3600.0)
            return None
        # ① hash 去重闸：与最近一帧无实质变化 → 丢弃
        if msg.hash and self._last_hash and not _hash_changed(msg.hash, self._last_hash):
            return None
        if msg.hash:
            self._last_hash = msg.hash
        # ② 规则匹配（本地 OCR 文本；无 ocr 字段 = OCR 路 · 不可用）
        ocr_text = " ".join(line.text for line in (msg.ocr or []))
        rule = self.rules.match(ocr_text) if ocr_text else None
        if rule is None:
            return self._on_unmatched(msg)
        self.rules.note_fired(rule)
        context: Optional[str]
        if rule.route == "text" and _text_sufficient(msg.ocr):
            context = ocr_text                       # 🔑 纯文本路：VLM 不调用（近零成本）
            logger.info("识屏文本路命中（规则 %s），VLM 跳过", rule.name)
        else:
            context = await self._vlm_observe(msg)   # image 路由 / 文本不充分 → 语境路
        if not context:
            return None                              # ③ VLM 超时/失败/SKIP → 跳过上下文
        return await self._speak(f"（屏幕内容：{context}）请就此自然地对用户说一句话。")

    def _on_unmatched(self, msg: ScreenShot) -> Optional[str]:
        """未命中：闲置嗅探静默丢弃；手动热键回落 M+2.3 通用截图信号（30s 冷却）。"""
        if msg.source == "hotkey" and self.behavior is not None:
            self.behavior.note_manual_screenshot()
        else:
            logger.info("识屏未命中规则，静默丢弃（source=%s）", msg.source)
        return None

    async def _vlm_observe(self, msg: ScreenShot, prompt: Optional[str] = None,
                           source: str = "vision") -> Optional[str]:
        try:
            observation = await asyncio.wait_for(
                self.vlm.describe(msg.image, prompt or self.observe_prompt,
                                  source=source),
                timeout=VLM_TIMEOUT_S)
        except Exception:
            logger.info("VLM 超时/失败 → 跳过屏幕上下文（识屏静默忽略）")
            return None
        text = (observation or "").strip()
        if not text or text.upper() == "SKIP":
            return None
        return text

    async def _handle_companion(self, msg: ScreenShot) -> Optional[str]:
        """陪玩分支（M-PLAY）：独立冷却（不动闲置识屏 4h 冷却）→ hash 去重 →
        VLM 一次调用两用（走 build_vlm 工厂——本地模型即插拔点）→ gate 照过。

        gate 拒不吞陪玩冷却额度（与闲置识屏同一哲学：冷却只记在真正搭话上）；
        观察与上下文全内存态用完即弃，不落盘。"""
        mono = self._clock()
        if mono - self._last_companion < self._companion_cooldown_s:
            logger.debug("陪玩服务端冷却中，丢弃")
            return None
        if msg.hash and self._last_hash and not _hash_changed(msg.hash, self._last_hash):
            return None                              # 同场景重复帧（客户端节流兜底）
        if msg.hash:
            self._last_hash = msg.hash
        context = await self._vlm_observe(msg, self._companion_prompt,
                                          source="companion")
        if not context:
            return None                              # SKIP / VLM 失败 → 静默
        if self.behavior is None:
            return None
        topic = f"（你们正一起看这个画面：{context}）请就此自然地吐槽或说一句话。"
        try:
            spoken = await self.behavior.request_screen_speak(
                topic, source="companion")
        except Exception:
            logger.warning("陪玩话题 gate 评估异常（fail-closed）", exc_info=True)
            return None
        if not spoken:
            logger.info("陪玩话题未过 gate（DND/白名单/冷却/时机），丢弃")
            return None
        self._last_companion = mono
        self._notify("陪玩：基于画面发了句吐槽")
        return topic

    async def _speak(self, topic: str) -> Optional[str]:
        """④ 话题 → behavior 全量 gate → 搭话。

        独立 4h 冷却只记在**真正搭话**上：gate 拒（DND/白名单/时机）不吞额度，
        话题本身用完即弃——重复触发重新评估（行为侧全局冷却仍兜底防连发）。
        """
        if self.behavior is None:
            return None
        try:
            spoken = await self.behavior.request_screen_speak(topic)
        except Exception:
            logger.warning("识屏话题 gate 评估异常（fail-closed）", exc_info=True)
            return None
        if not spoken:
            logger.info("识屏话题未过 gate（DND/白名单/冷却/时机），丢弃（用完即弃）")
            return None
        self._last_submit = self._clock()
        logger.info("识屏搭话成立（独立 4h 冷却已记）")
        self._notify("识屏：基于屏幕内容发起了主动搭话")
        return topic
