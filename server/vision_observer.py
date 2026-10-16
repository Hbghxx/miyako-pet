"""摄像头感知观察器（M-CAM/VIS.5）—— 帧 → VLM 观察 → mini-gate → 主动搭话。

mini-gate（M+2.3 全量 gate 落地前的降级版，plan M-CAM 定案）：
  cooldown（两次主动搭话最小间隔）+ 每日上限 + orchestrator 空闲；DND 由客户端
  **源头停发**（本模块不重复判定）。VLM 失败/SKIP/忙碌一律静默丢弃。

隐私硬约束：帧与观察文本**内存态用完即弃，不落 memory.db**；观察内容仅作为
trigger:"behavior" 的上下文注入一次对话。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_OBSERVE_PROMPT = ("用不超过40字描述画面中用户的状态与情绪；"
                          "没有值得注意的变化只输出 SKIP。")


class VisionObserver:
    def __init__(self, *, orchestrator, vlm,
                 settings: Optional[dict] = None,
                 clock: Callable[[], float] = time.time,
                 notify: Optional[Callable[[str], None]] = None) -> None:
        vision = (settings or {}).get("vision") or {}
        self.observe_prompt = str(vision.get("observe_prompt")
                                  or DEFAULT_OBSERVE_PROMPT)
        self.cooldown_s = max(0.0, float(vision.get("cooldown_min", 10)) * 60.0)
        self.max_per_day = max(1, int(vision.get("max_per_day", 5)))
        self.orchestrator = orchestrator
        self.vlm = vlm
        self._clock = clock
        self.notify = notify or (lambda msg: logger.info("%s", msg))
        self._last = -1e18
        self._day = ""
        self._count = 0
        self._inflight = False

    def _bump_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self._day:
            self._day = day
            self._count = 0

    def _gate_ok(self, now: float) -> bool:
        self._bump_day(now)
        if now - self._last < self.cooldown_s:
            return False
        if self._count >= self.max_per_day:
            return False
        return True

    async def submit(self, image_b64: str) -> None:
        """处理一帧：gate 预检 → VLM 观察 → gate 复检 → 注入 behavior 对话。"""
        if self._inflight or self.orchestrator.is_busy:
            return                                   # reply 忙/上一帧观察中 → 丢弃
        now = self._clock()
        if not self._gate_ok(now):
            return
        self._inflight = True
        try:
            observation = await self.vlm.describe(image_b64, self.observe_prompt,
                                                  source="vision")
        except Exception:
            logger.debug("VLM 观察失败，丢弃当帧", exc_info=True)
            return
        finally:
            self._inflight = False
        text = (observation or "").strip()
        if not text or text.upper() == "SKIP":
            return                                   # 无话可说
        now = self._clock()
        if not self._gate_ok(now):                   # VLM 调用耗时后复检
            return
        # 受理才计冷却/配额：忙碌被丢弃时不再白扣「每日上限」额度（与识屏
        # 路由同一哲学——冷却只记在真正搭话上）
        if not self.orchestrator.submit_behavior(
                f"（摄像头观察：{text}）请就此自然地对用户说一句关心的话。"):
            logger.debug("reply 忙，摄像头观察话题被丢弃（不计配额）")
            return
        self._bump_day(now)
        self._count += 1
        self._last = now
        self.notify(f"摄像头感知：已基于观察发起主动搭话（今日第 {self._count} 次）")
