"""主动搭话分寸（M+2.3）—— 五类信号源 + 统一 _should_speak gate。

信号源（plan 定版间隔；「时段」默认开，其余按 plan 注记）：
  greeting      时段问候，每日 1 次（跨过配置小时后的第一次 tick）
  memory_hook   记忆钩子，8h（数据源 = 合并调用 open_question → hooks 表）
  screenshot    手动截屏，30s 冷却（触发源 note_manual_screenshot，热键 M+3 接线）
  idle_sniff    闲置嗅探，4h，默认关（原则 6/7；server 侧以最近一次用户输入计时）
  open_topic    未回扣话题，2h（数据源同上：追问一次即消费；用户回扣也消费）

统一 gate `_should_speak`（顺序 plan 定版，固定）：
  DND → 全屏白名单 → 冷却 → 信号强度 → emotion 槽 LLM 时机判定。
  🔑 LLM 失败/输出不明确 → 不搭话（fail-closed）；gate 内任何异常 → 不搭话；
  无 state 数据（foreground 为 null）→ 不搭话。

信号强度消费 [reflect] 条目（v1.9.2/M+2.3）：近 7 天反思越多越收敛（调档）。
主动搭话经注入的 submit（orchestrator.submit_behavior → reply_begin{trigger:"behavior"}）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable, Optional

from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_REFLECT_LOOKBACK_DAYS = 7      # 近 7 天反思参与调档
_REFLECT_PENALTY = 0.1          # 每条反思的强度衰减
_STRENGTH_FLOOR = 0.2           # 调档下限（再多的反思也不至于永久哑火）
_HOOK_ECHO_SIM_THRESHOLD = 0.25  # 「回扣」判定：用户文本与 hook 的 bigram 命中率


def _now() -> datetime:
    return datetime.now()


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None


def should_send_state(last_key, new_key, elapsed_s: float, min_gap_s: float = 5.0) -> bool:
    """M+2.2 上报节流纯函数：变更触发 + 最小间隔 5s。"""
    if last_key != new_key:
        return elapsed_s >= min_gap_s
    return False


def signal_strength(base: float, reflect_count: int) -> float:
    """信号强度调档（M+2.3 消费 [reflect]）：反思越多越收敛，钳到下限。"""
    return max(_STRENGTH_FLOOR, base - _REFLECT_PENALTY * max(0, reflect_count))


class Behavior:
    """主动搭话调度器。submit 注入 orchestrator.submit_behavior（trigger="behavior"）。"""

    _SIGNAL_BASE = {"greeting": 0.5, "memory_hook": 0.7, "screenshot": 0.5,
                    "idle_sniff": 0.5, "open_topic": 0.6}

    def __init__(self, *, store: MemoryStore, llm, submit: Callable[[str], None],
                 state_getter: Callable[[], dict],
                 settings: Optional[dict] = None,
                 clock: Optional[Callable[[], float]] = None,
                 now_fn: Callable[[], datetime] = _now,
                 keyword_sim: Optional[Callable[[str, str], float]] = None) -> None:
        self.store = store
        self.llm = llm                      # emotion 槽（时机判定）
        self._submit = submit
        self._state_getter = state_getter
        cfg = dict(settings or {})
        # 配置 schema：主动搭话参数嵌套在 behavior.proactive 下（settings.example.yaml
        # 与设置窗写回同构）；平面键是单测/旧配置形态——嵌套存在时合并覆盖平面值。
        # 此前只读平面键 → 用户配置（如 cooldown_min_gap_s: 120、总开关 false）
        # 全部静默失效，按默认值跑。
        proactive = cfg.get("proactive")
        if isinstance(proactive, dict):
            cfg.update(proactive)
        self.enabled = bool(cfg.get("enabled", True))
        self.cooldown_min_gap_s = float(cfg.get("cooldown_min_gap_s", 600))
        self.whitelist = [str(x).lower() for x in (cfg.get("fullscreen_whitelist") or [])]
        greeting = cfg.get("greeting") or {}
        self.greeting_enabled = bool(greeting.get("enabled", True))
        self.greeting_hour = int(greeting.get("hour", 9))
        self.greeting_text = str(greeting.get("text") or "早上呀！新的一天也请多指教。")
        self.memory_hook_hours = float(cfg.get("memory_hook_hours", 8))
        self.open_topic_hours = float(cfg.get("open_topic_hours", 2))
        self.screenshot_cooldown_s = float(cfg.get("screenshot_cooldown_s", 30))
        self.idle_sniff_enabled = bool((cfg.get("idle_sniff") or {}).get("enabled", False))
        self.idle_hours = float((cfg.get("idle_sniff") or {}).get("hours", 4))
        self.strength_threshold = float(cfg.get("strength_threshold", 0.4))
        self._clock = clock or time.monotonic
        self._now_fn = now_fn
        if keyword_sim is None:
            # 回扣判定复用检索命中率语义（局部导入：单测可不触检索模块）
            from server.memory.vector import keyword_sim as _default_sim

            keyword_sim = _default_sim
        self._keyword_sim = keyword_sim
        self._last_fired: dict[str, float] = {}          # 信号名 → monotonic
        self._global_last: Optional[float] = None        # 任意信号的全局最小间隔
        self._last_greeting_date: Optional[str] = None
        self._last_user_text: Optional[datetime] = None  # 闲置嗅探计时源

    # ---------------- 外部输入 ----------------
    def on_state_changed(self, state: dict) -> None:
        """orchestrator 转发的 state 上报回调（M+2.2）；gate 读值经 state_getter。"""

    def note_user_message(self, text: str) -> None:
        """用户发言：刷新闲置计时 + 未回扣话题的「回扣」判定（命中即消费钩子）。"""
        self._last_user_text = self._now_fn()
        try:
            for hook in self.store.pending_hooks():
                if self._keyword_sim(text, hook["question"]) >= _HOOK_ECHO_SIM_THRESHOLD:
                    self.store.mark_hook_consumed(hook["id"])
                    logger.info("话题已回扣，钩子 #%s 消费（不再追问）", hook["id"])
        except Exception:
            logger.debug("回扣判定失败（不影响用户消息）", exc_info=True)

    def note_manual_screenshot(self) -> None:
        """手动截屏触发（30s 冷却经 gate；热键接线在 M+3）。"""
        self._last_fired["screenshot_request"] = self._clock()

    async def request_screen_speak(self, text: str,
                                   source: str = "screen") -> bool:
        """识屏话题（M+3.3 路由④）：**全量 gate 即时评估**，通过才搭话。

        复用 _should_speak 全序（DND→白名单→全局冷却→[reflect] 强度→LLM 时机），
        任何异常/不明确 → 不搭话（fail-closed，与 tick 路径同一纪律）。
        识屏的独立 4h 冷却归 ScreenRouter 管（M+3.3），此处不记 screen 槽位。
        source：会话 token 统计来源桶（screen=识屏 / companion=陪玩）。
        """
        try:
            if not self.enabled:
                return False
            if not self._should_speak("screen"):
                return False
            if not await self._llm_timing_ok("screen", self._now_fn()):
                return False
            self._global_last = self._clock()
            logger.info("主动搭话（识屏）：%s", text[:40])
            self._submit(text, source)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("识屏搭话评估异常（fail-closed）", exc_info=True)
            return False

    # ---------------- 调度 ----------------
    async def tick(self) -> Optional[str]:
        """评估一轮信号；gate 全过则搭话（submit）并返回文本，否则 None。绝不抛异常。"""
        try:
            return await self._tick_inner()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("主动搭话评估异常（本轮放弃）", exc_info=True)
            return None

    async def _tick_inner(self) -> Optional[str]:
        if not self.enabled:
            return None
        now = self._now_fn()
        mono = self._clock()
        signal = self._pick_signal(now, mono)
        if signal is None:
            return None
        name, text, hook_id = signal
        if not self._should_speak(name):
            return None
        if not await self._llm_timing_ok(name, now):
            return None
        self._last_fired[name] = mono
        self._global_last = mono
        if hook_id is not None:
            try:
                self.store.mark_hook_consumed(hook_id)   # 追问/提及一次即消费
            except Exception:
                logger.debug("钩子消费标记失败", exc_info=True)
        if name == "greeting":
            self._last_greeting_date = now.date().isoformat()
        logger.info("主动搭话（%s）：%s", name, text[:40])
        self._submit(text)
        return text

    # ---------------- gate（顺序 plan 定版，任何异常 fail-closed） ----------------
    def _should_speak(self, signal: str) -> bool:
        try:
            mono = self._clock()
            if self._global_last is not None and \
                    mono - self._global_last < self.cooldown_min_gap_s:
                return False                                   # 全局冷却
            state = dict(self._state_getter() or {})
            if state.get("dnd"):
                return False                                   # ① DND
            foreground = state.get("foreground")
            if foreground is None:
                return False                                   # ② 无 state 数据 fail-closed
            if state.get("locked"):
                return False                                   # 锁屏不打扰
            process = str((foreground or {}).get("process") or "").lower()
            if self.whitelist and process in self.whitelist:
                return False                                   # ③ 全屏白名单应用零搭话
            base = self._SIGNAL_BASE.get(signal, 0.5)
            strength = signal_strength(base, self._reflect_count())
            if strength < self.strength_threshold:
                return False                                   # ④ 信号强度（[reflect] 调档）
            return True
        except Exception:
            logger.warning("gate 异常 → 不搭话（fail-closed）", exc_info=True)
            return False

    async def _llm_timing_ok(self, signal: str, now: datetime) -> bool:
        """emotion 槽时机判定：只认「是」；失败/不明确一律不搭话（fail-closed）。"""
        try:
            prompt = (f"现在是 {now.strftime('%H:%M')}。触发信号：{signal}。"
                      "判断此刻主动和用户说话是否合适，只回答「是」或「否」。")
            output = "".join([chunk async for chunk in
                              self.llm.stream_lines(prompt)]).strip()
            return output.startswith("是")
        except Exception:
            logger.warning("时机判定 LLM 失败 → 不搭话（fail-closed）", exc_info=True)
            return False

    def _reflect_count(self) -> int:
        """近 7 天 [reflect] 条数（emotion_tone 承载，v1.9.2 前缀约定）。"""
        cutoff = self._now_fn().timestamp() - _REFLECT_LOOKBACK_DAYS * 86400
        count = 0
        for row in self.store.entries(kinds=["emotion_tone"]):
            ts = _parse_iso(row["ts"])
            content = str(row["content"])
            if ts is not None and ts.timestamp() >= cutoff and content.startswith("[reflect]"):
                count += 1
        return count

    # ---------------- 信号源（返回 (name, text, related_hook_id)） ----------------
    def _pick_signal(self, now: datetime, mono: float) -> Optional[tuple[str, str, Optional[int]]]:
        candidates: list[tuple[str, str, Optional[int]]] = []
        # greeting：每日 1 次（跨过配置小时的第一次）
        if self.greeting_enabled and now.hour >= self.greeting_hour \
                and self._last_greeting_date != now.date().isoformat():
            candidates.append(("greeting", self.greeting_text, None))
        for hook in self.store.pending_hooks():
            created = _parse_iso(hook["created_at"])
            if created is None:
                continue
            age_h = (now - created).total_seconds() / 3600.0
            # 阈值序：先判 memory_hook（默认 8h）再判 open_topic（默认 2h）——
            # 反序时第二分支要求 age∈[8h,2h) 恒假，memory_hook 永不可达（死分支）；
            # 不够旧的钩子继续看下一条（保持原扫描语义）
            if age_h >= self.memory_hook_hours:
                candidates.append(("memory_hook",
                                   f"忽然想起你说过「{hook['question']}」，还惦记着呢。",
                                   int(hook["id"])))
                break
            if age_h >= self.open_topic_hours:
                candidates.append(("open_topic",
                                   f"对了，之前你提到「{hook['question']}」，后来怎么样了？",
                                   int(hook["id"])))
                break
        if "screenshot_request" in self._last_fired:
            # 手动截屏触发：30s 冷却由 gate 间隔管；消费后转一次性信号
            candidates.append(("screenshot", "要看看屏幕里有什么吗？", None))
        if self.idle_sniff_enabled and self._last_user_text is not None:
            idle_h = (now - self._last_user_text).total_seconds() / 3600.0
            if idle_h >= self.idle_hours:
                candidates.append(("idle_sniff", "还在忙吗？我也在呢。", None))
        # 各信号自身间隔（默认 1h 兜底）+ 手动触发的一次性消费
        intervals = {"greeting": 86400.0, "memory_hook": self.memory_hook_hours * 3600,
                     "screenshot": self.screenshot_cooldown_s,
                     "idle_sniff": self.idle_hours * 3600,
                     "open_topic": self.open_topic_hours * 3600}
        for name, text, hook_id in candidates:
            last = self._last_fired.get(name)
            if last is not None and mono - last < intervals.get(name, 3600.0):
                continue
            if name == "screenshot":
                self._last_fired.pop("screenshot_request", None)
            return name, text, hook_id
        return None
