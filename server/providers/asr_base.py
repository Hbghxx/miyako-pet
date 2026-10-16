"""ASR provider 抽象（M1.7）。输入 16kHz 单声道 PCM16；失败语义见 M2.3（Error{scope:"asr"}）。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class AsrBase(ABC):
    name = "base"

    @abstractmethod
    async def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> str:
        """语音转文本。"""
