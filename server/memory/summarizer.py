"""角色日记与行为反思（M+1.5）—— diary 槽 + [reflect] 条目（v1.9.2）+ 每日校对委托。

run_daily 流程（每日触发器调用）：
  ① 每日校对委托 deduplicator.run()（先清洗再写日记，避免把矛盾条目写进日记素材）；
  ② 收集近 1 天素材（无素材 → 跳过不写）；
  ③ diary 槽生成日记（第一人称、≤200 字、按 .char 人设语气、禁止出现「用户」一词
     ——含则**重新生成一次**（借鉴⑤修复重试语义），仍含 → 当日日记弃用不写）；
  ④ 顺带产出 0~2 条 [reflect] 行为反思（emotion_tone 类承载、代码强制 [reflect]
     前缀、条数钳制；供 behavior gate 信号强度消费，M+2.3）；
  ⑤ 全部经持久化写队列落库（借鉴①）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

REFLECT_PREFIX = "[reflect]"
MAX_REFLECTS = 2
DIARY_MAX_CHARS = 200

_DIARY_INSTRUCTION = (
    "以下是你最近一天与用户相处时记下的事实素材。请以第一人称写一篇不超过 200 字的"
    "日记（按你的人设语气）。之后另起行，输出 0~2 条以 [reflect] 开头的行为反思"
    "（每条一句话，反思你自己的说话时机与方式是否得当）。\n"
    "注意：日记与反思里都不要出现「用户」这个词（可以用称呼代替）。\n\n素材：\n"
)
_DIARY_REPAIR = (
    "你上一条输出里出现了「用户」这个词。重新输出：第一人称日记（≤200 字）+ "
    "另起行的 0~2 条 [reflect] 反思，全文不要出现「用户」。"
)


def _split_diary_reflects(output: str) -> tuple[str, list[str]]:
    """拆分输出：非 [reflect] 行 = 日记正文；[reflect] 行 = 反思条目（钳 2 条）。"""
    diary_lines: list[str] = []
    reflects: list[str] = []
    for ln in (output or "").splitlines():
        t = ln.strip()
        if not t:
            continue
        if t.startswith(REFLECT_PREFIX):
            if len(reflects) < MAX_REFLECTS:      # 条数钳制（代码强制，不靠模型自觉）
                reflects.append(t)
        else:
            diary_lines.append(t)
    return "\n".join(diary_lines).strip(), reflects


class Summarizer:
    """日记生成器 + 每日维护入口。llm 为 diary 槽（stream_lines 契约，含修复重试）。"""

    def __init__(self, store, llm, deduplicator=None, *,
                 now: Optional[Callable[[], datetime]] = None,
                 notify: Optional[Callable[[str], None]] = None) -> None:
        self.store = store
        self.llm = llm
        self.deduplicator = deduplicator
        self._now = now or (lambda: datetime.now())
        self.notify = notify or (lambda msg: logger.info("%s", msg))

    async def run_daily(self) -> Optional[dict]:
        """跑一轮每日维护；返回 {"diary": str, "reflects": [str]}，无素材/弃用返回 None。"""
        # ① 每日校对委托（M+1.5 定案：先清洗，再写日记）
        if self.deduplicator is not None:
            try:
                await self.deduplicator.run(days=3.0)
            except Exception:
                logger.warning("每日校对委托失败（日记流程继续）", exc_info=True)

        facts = self.store.facts_recent(1.0)
        if not facts:
            logger.info("日记跳过：近 1 天无素材")
            return None
        material = "\n".join(f"- {row['content']}" for row in facts)
        prompt = _DIARY_INSTRUCTION + material

        diary_text = ""
        reflects: list[str] = []
        discarded = False
        try:
            for attempt in range(2):              # 「用户」检查：含则重生成一次，再含弃用
                output = "".join([chunk async for chunk in
                                  self.llm.stream_lines(prompt)])
                diary_text, reflects = _split_diary_reflects(output)
                if diary_text and "用户" not in diary_text and all(
                        "用户" not in r for r in reflects):
                    break
                if attempt == 0:
                    prompt = _DIARY_REPAIR + "\n\n素材：\n" + material
                else:
                    discarded = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("日记生成调用失败（静默跳过当日）", exc_info=True)
            return None
        if discarded or not diary_text:
            self.notify("日记含「用户」一词且重生成后仍如此——当日日记弃用不写")
            return None

        now = self._now()
        day = now.strftime("%Y-%m-%d")
        self.store.enqueue_write(kind="diary", content=diary_text,
                                 importance=0.4, source="summarizer",
                                 created_at=now.isoformat(timespec="seconds"))
        for r in reflects:
            self.store.enqueue_write(kind="emotion_tone", content=r,
                                     importance=0.3, source="summarizer",
                                     created_at=now.isoformat(timespec="seconds"))
        self.store.flush_pending()
        self.notify(f"日记已写入（{day}，附反思 {len(reflects)} 条）")
        return {"diary": diary_text, "reflects": reflects}
