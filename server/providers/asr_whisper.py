"""faster-whisper ASR provider（M2.3）—— 本地 CPU int8，lazy import（extras: asr）。

失败语义（降级矩阵 ASR 行）：依赖缺失/模型加载/转写任何失败 → 抛 AsrError →
orchestrator 既有路径捕获并发 Error{scope:"asr"} +「没听清」（M1.9，不在此重复）。
模型首次使用时加载并缓存（首启可能联网下载权重，preflight 有对应 ⚠️ 项）。
输入 16kHz 单声道 PCM16；其它采样率先线性重采样到 16k（纯 numpy，无新依赖）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import numpy as np

from server.providers.asr_base import AsrBase

logger = logging.getLogger(__name__)

TARGET_SR = 16000

# whisper 对非语音的经典幻觉短语（中文环境最高频集合；启发式短名单，按需增补）
_HALLUCINATION_PHRASES = ("谢谢观看", "字幕由", "请不吝点赞", "订阅我们的频道",
                          "明镜与三五")


class AsrError(RuntimeError):
    """ASR 失败——orchestrator 捕获后 Error{scope:"asr"}，文本置空（「没听清」）。"""


def _linear_resample(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr or data.size == 0:
        return data
    duration = data.size / float(src_sr)
    dst_size = max(1, int(round(duration * dst_sr)))
    src_idx = np.linspace(0.0, data.size - 1, num=dst_size)
    return np.interp(src_idx, np.arange(data.size), data).astype(np.float32)


class AsrWhisper(AsrBase):
    name = "whisper"

    def __init__(self, config: Optional[dict] = None, *,
                 model=None,
                 model_loader: Optional[Callable[[], object]] = None) -> None:
        asr_cfg = (config or {}).get("asr") or {}
        self.model_size = str(asr_cfg.get("model_size", "small"))
        self.device = str(asr_cfg.get("device", "cpu"))
        self.compute_type = str(asr_cfg.get("compute_type", "int8"))
        self.language = asr_cfg.get("language") or None   # None=自动检测（可配置）
        self.beam_size = int(asr_cfg.get("beam_size", 5))
        # 幻听防线（v1.9.4）：Silero VAD 预过滤非语音 + 段级置信度闸 + 幻觉短语
        self.vad_filter = bool(asr_cfg.get("vad_filter", True))
        self.no_speech_gate = float(asr_cfg.get("no_speech_prob", 0.6))
        self.avg_logprob_min = float(asr_cfg.get("avg_logprob_min", -1.0))
        self._model = model                    # 测试注入；生产首次使用时加载
        self._model_loader = model_loader or self._default_loader

    def _default_loader(self):
        from faster_whisper import WhisperModel  # lazy（extras: asr）

        return WhisperModel(self.model_size, device=self.device,
                            compute_type=self.compute_type)

    def _ensure_model(self):
        if self._model is None:
            logger.info("加载 faster-whisper：%s/%s/%s（首次可能下载权重）",
                        self.model_size, self.device, self.compute_type)
            self._model = self._model_loader()
        return self._model

    def _transcribe_sync(self, audio_f32: np.ndarray) -> str:
        model = self._ensure_model()
        segments, _info = model.transcribe(
            audio_f32, language=self.language, beam_size=self.beam_size,
            vad_filter=self.vad_filter,                # 幻听防线：非语音段预剔除
            condition_on_previous_text=False)          # 防幻觉跨段携带
        parts: list[str] = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            no_speech = float(getattr(seg, "no_speech_prob", 0.0))
            logprob = float(getattr(seg, "avg_logprob", 0.0))
            if no_speech > self.no_speech_gate:
                logger.info("丢弃无语音段（no_speech_prob=%.2f > %.2f）: %r",
                            no_speech, self.no_speech_gate, text)
                continue
            if logprob < self.avg_logprob_min:
                logger.info("丢弃低置信段（avg_logprob=%.2f < %.2f）: %r",
                            logprob, self.avg_logprob_min, text)
                continue
            if any(phrase in text for phrase in _HALLUCINATION_PHRASES):
                logger.info("丢弃幻觉短语段: %r", text)
                continue
            parts.append(text)
        return "".join(parts).strip()

    async def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> str:
        if not pcm16:
            return ""
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32767.0
        audio = _linear_resample(audio, int(sample_rate), TARGET_SR)
        try:
            # 模型推理是 CPU 密集同步调用 → 丢线程池，不堵事件循环（WS/TTS 并发）
            return await asyncio.to_thread(self._transcribe_sync, audio)
        except Exception as exc:
            raise AsrError(f"faster-whisper 转写失败：{exc}") from exc
