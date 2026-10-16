"""VLM provider 抽象（M1.7 / M+3.3 识屏消费）。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class VlmBase(ABC):
    name = "base"

    @abstractmethod
    async def describe(self, image_b64: str, text_hint: str | None = None,
                       source: str = "vision") -> str:
        """看图描述；text_hint 为 OCR 文本提示（纯文本路时 VLM 不应被调用，M+3.3）。

        source：会话 token 统计来源桶（vision=摄像头/闲置识屏 /
        companion=陪玩画面理解）。
        """
