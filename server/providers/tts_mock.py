"""Mock TTS（M1.7）：1kHz 正弦波 PCM16，时长随文本长度变化，零外部依赖。

``silent=True`` 形态（2026-09-18 补收敛漏网）：**降级落点专用的纯气泡**——
配置了音色级（gptsovits/cloud_clone）但探活/合成失败落到链尾 mock 时，
发空流（AudioBegin+AudioEnd 零块）= 纯气泡，不出正弦滴声（「听错误音色
不如纯文本」定案；v0.8.0 release notes「未启动=纯文本」的兑现）。
出厂 `providers.tts: mock`（CI/开发期 UI 手感）保持有声驱动口型与音频链。
"""
from __future__ import annotations

from typing import AsyncIterator

import numpy as np

from server.providers.tts_base import TtsBase

SAMPLE_RATE = 16000
TONE_HZ = 1000
SECONDS_PER_CHAR = 0.09
MIN_SECONDS = 0.3
CHUNK_BYTES = 4096  # ≈128ms


class MockTts(TtsBase):
    name = "mock"
    sample_rate = SAMPLE_RATE

    def __init__(self, *, silent: bool = False) -> None:
        self.silent = silent

    async def stream(self, text_ja: str, emotion: str | None = None) -> AsyncIterator[bytes]:
        if self.silent:
            return                          # 纯气泡：AudioBegin+AudioEnd 零音频块
        duration = max(MIN_SECONDS, len(text_ja or "") * SECONDS_PER_CHAR)
        samples = int(duration * SAMPLE_RATE)
        t = np.arange(samples, dtype=np.float64) / SAMPLE_RATE
        wave = (0.5 * 32767.0 * np.sin(2.0 * np.pi * TONE_HZ * t)).astype("<i2")
        pcm = wave.tobytes()
        for offset in range(0, len(pcm), CHUNK_BYTES):
            yield pcm[offset:offset + CHUNK_BYTES]
