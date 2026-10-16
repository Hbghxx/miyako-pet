"""Mock ASR（M1.7）：固定句子，供 mock 链路跑通语音→文本路径。"""
from __future__ import annotations

from server.providers.asr_base import AsrBase

MOCK_TRANSCRIPT = "今日はいい天気だね、少し散歩しない？"


class MockAsr(AsrBase):
    name = "mock"

    async def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> str:
        return MOCK_TRANSCRIPT
