"""ToolAnnouncer（M-TOOL）—— 工具模式播报编排。

职责（域隔离定案，plan M-TOOL）：
· kind → 触发上下文模板：触发文本作为 user_text 注入 LLM，角色按 persona
  口吻播报（与 behavior 固定台词同款机制——台词由 LLM 生成，不硬编码）；
· submit(kind, detail)：orchestrator 忙碌时入队（cap 2，超限丢最旧——积压
  的提醒没有补播价值），空闲时经 submit_tool 发起 trigger:"tool" 播报轮；
· drain：挂在 orchestrator.on_reply_done 钩子上——任何一轮 reply 结束
  （含用户闲聊轮被打断后的正常轮）后补播队列中的待播事件。

播报轮语义由 orchestrator 的 trigger=="tool" 守卫保证（只读 persona：不检索
记忆/不进历史/不写任何状态），本模块不重复实现、也不知道这些细节。
全部无视 DND：工具事件不经过 behavior gate（用户履约与定时提醒不是主动搭话）。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque

logger = logging.getLogger(__name__)

# 占线队列上限（M-TOOL 定案）：超过丢最旧
QUEUE_CAP = 2

# kind → 触发上下文模板（{detail} 占位；detail 为客户端人读补充，如 "07:30"）。
# 措辞约定：引导 LLM 输出一两句符合角色的播报，模板自身不输出。
_TOOL_CONTEXT_TEMPLATES = {
    "pomodoro_work_end":
        "（工具事件：专注 {detail} 分钟的番茄钟计时结束了。请用一两句话提醒用户休息一下。）",
    "pomodoro_break_end":
        "（工具事件：番茄钟的休息时间结束了。请用一句话邀请用户开始下一段专注。）",
    "alarm":
        "（工具事件：设定的 {detail} 闹钟时间到了。请提醒用户。）",
    "countdown":
        "（工具事件：{detail} 分钟的倒计时结束了。请提醒用户。）",
    "water":
        "（工具事件：距上次提醒已经过了 {detail} 分钟。请用一句话提醒用户喝水。）",
    "sedentary":
        "（工具事件：用户已经连续伏案约 {detail} 分钟了。请用一句话提醒用户起身活动一下。）",
    "hourly":
        "（工具事件：现在是{detail}。请用一句话向用户报时。）",
    "festival":
        "（工具事件：今天是{detail}。请向用户送上节日问候。）",
}


class ToolAnnouncer:
    """工具事件 → 播报轮编排（见模块 docstring）。仅事件循环线程调用。"""

    def __init__(self, orchestrator, *, queue_cap: int = QUEUE_CAP) -> None:
        self._orch = orchestrator
        self._cap = max(1, int(queue_cap))
        self._queue: deque[tuple[str, str]] = deque()
        orchestrator.on_reply_done = self._on_reply_done

    def _context_text(self, kind: str, detail: str) -> str:
        template = _TOOL_CONTEXT_TEMPLATES.get(kind)
        if template is None:      # 协议层 Literal 已拦未知 kind；此处防御兜底
            logger.warning("未知工具事件 kind=%r，忽略", kind)
            return ""
        try:
            return template.format(detail=detail)
        except Exception:
            logger.warning("工具事件模板渲染失败 kind=%r detail=%r", kind, detail,
                           exc_info=True)
            return ""

    def submit(self, kind: str, detail: str = "") -> bool:
        """提交一个工具事件：闲时立即发起播报轮（True）；忙碌入队（False）。"""
        text = self._context_text(kind, detail)
        if not text:
            return False
        if not self._orch.submit_tool(text):
            if len(self._queue) >= self._cap:
                dropped = self._queue.popleft()
                logger.info("工具播报队列已满（cap=%d），丢弃最旧 %s",
                            self._cap, dropped[0])
            self._queue.append((kind, detail))
            return False
        return True

    def _on_reply_done(self) -> None:
        """orchestrator.on_reply_done 钩子（ReplyEnd 后、reply 任务体内）。

        此刻 is_busy 仍为 True（任务体尚未返回），须推迟到任务完成后 drain，
        submit_tool 才能受理——call_soon 一次即可。
        """
        if not self._queue:
            return
        try:
            asyncio.get_running_loop().call_soon(self._drain)
        except RuntimeError:
            self._drain()   # 无运行循环（测试直调）：同步尝试

    def _drain(self) -> None:
        while self._queue:
            kind, detail = self._queue[0]
            if self._orch.submit_tool(self._context_text(kind, detail)):
                self._queue.popleft()
            else:
                break   # 仍忙：留在队首，等下一轮 reply 结束再补播

    @property
    def pending(self) -> int:
        """待播队列长度（测试与诊断用）。"""
        return len(self._queue)
