"""LLM provider 抽象（M1.7 / 原则 1：模型层完全可插拔）。

产出契约：``stream_lines`` 逐行 yield **原始行**（可能不带换行、可能多行粘连），
由 orchestrator 的行缓冲切分后交给 OutputContract 解析（M1.9）——
provider 不自行做行缓冲（原则 11：同一关注点不拆两个模块）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class LlmBase(ABC):
    name = "base"

    @abstractmethod
    async def stream_lines(self, user_text: str, *,
                           history: list[tuple[str, str]] | None = None
                           ) -> AsyncIterator[str]:
        """流式产出原始输出行。history 为 [(说话方, 文本)] 的近端对话序列。"""
        # 具体实现必须支持 async with 取消：orchestrator 收到 Interrupt 会 cancel 任务。
        yield ""  # pragma: no cover - 抽象占位
