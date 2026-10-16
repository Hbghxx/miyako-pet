"""TTS provider 抽象（M1.7）。

产出契约：``stream`` 按块 yield PCM16 字节（int16, little-endian）；
``sample_rate`` 必须如实上报实际采样率（AudioBegin 载荷）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class TtsBase(ABC):
    name = "base"
    sample_rate: int = 16000

    @abstractmethod
    async def stream(self, text_ja: str, emotion: str | None = None) -> AsyncIterator[bytes]:
        """合成/播放 text_ja，按块产出 PCM16 字节（§0.4：TTS 合成源=text_ja）。

        emotion（M+1.6）：供 ③级 audiofile 按情绪选预录音（其余实现忽略）。
        """
        yield b""  # pragma: no cover - 抽象占位
