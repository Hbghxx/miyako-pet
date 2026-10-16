"""webrtc AEC 实装（M+1.5.1）—— lazy import 封装 + 10ms 帧对齐 + 并发锁。

帧对齐：far/near 各带内部字节缓冲，**攒够 10ms 帧**（frame = 采样率/100；
16k→160 样本、32k→320 样本，plan 公式同源）才喂 APM，不足部分留待下批。
process_mic 前 ``set_stream_delay_ms``（初值 120ms，AudioPlayback.stream_delay
信号按播放端 latency 更新——run_client 接线）。

🔑 并发：process_far（播放线程）与 process_mic（采集线程）并发，一把
``threading.Lock`` 包住 APM 入口 + 两个缓冲（APM 实例非线程安全，plan 定案）。

绑定适配：webrtc-audio-processing 的 Python 绑定方法名随版本变化，初始化时
按 preflight 同款候选表探测（reverse/process/delay 三类），全部缺失 → 抛错 →
工厂降级 aec_none（§4.1「AEC 缺失/初始化失败」行）。

测试缝：``apm_factory`` 注入——extras 未装的环境（Windows 无轮子）用脚本化
APM 验证帧对齐/延迟/并发/残差衰减；真机 APM 路径归人工介入点。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import numpy as np

from client.aec_base import AecBase

logger = logging.getLogger(__name__)

# 绑定方法名候选表（与 scripts/preflight.check_aec 同源策略）
_REVERSE_NAMES = ("process_reverse_capture_stream", "process_far_frame",
                  "ProcessFarFrame", "process_far", "reverse_stream")
_PROCESS_NAMES = ("process_stream", "ProcessStream", "process_mic",
                  "process_capture_stream")
_DELAY_NAMES = ("set_stream_delay_ms", "SetStreamDelayMs")


def _probe(obj, names):
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            return fn
    return None


class AecWebrtc(AecBase):
    """webrtc APM 封装。apm_factory 注入（测试）或 lazy import 真件（生产）。"""

    name = "webrtc"

    def __init__(self, config: Optional[dict] = None,
                 apm_factory: Optional[Callable[[], object]] = None) -> None:
        cfg = config or {}
        self._rate = int(cfg.get("sample_rate", 16000))
        self._frame_samples = max(1, self._rate // 100)      # 10ms 帧
        self._delay_ms = 120.0
        self._lock = threading.Lock()
        self._far_buf = bytearray()
        self._near_buf = bytearray()
        self._out_buf = bytearray()
        if apm_factory is not None:
            self._apm = apm_factory()
        else:
            self._apm = self._create_default_apm()
        self._reverse = _probe(self._apm, _REVERSE_NAMES)
        self._process = _probe(self._apm, _PROCESS_NAMES)
        self._set_delay = _probe(self._apm, _DELAY_NAMES)
        if self._process is None or self._reverse is None:
            raise AttributeError("APM 绑定缺少 far/near 处理入口（候选表全未命中）")

    def _create_default_apm(self):
        """真件构造：绑定 API 随版本漂移，逐候选尝试；失败上抛由工厂降级。"""
        import webrtc_audio_processing as wap

        for factory_name in ("AudioProcessingModule", "AudioProcessing",
                             "WebRtcAudioProcessing", "APM"):
            factory = getattr(wap, factory_name, None)
            if callable(factory):
                try:
                    return factory()
                except Exception:
                    logger.debug("APM 工厂 %s 构造失败", factory_name, exc_info=True)
        raise AttributeError("webrtc_audio_processing 无可用 APM 工厂")

    # ---------------- 延迟（播放端更新） ----------------
    def set_stream_delay(self, delay_ms: float) -> None:
        """AudioPlayback.stream_delay 信号按播放 latency 更新（run_client 接线）。"""
        with self._lock:
            self._delay_ms = float(delay_ms)

    # ---------------- AecBase 契约 ----------------
    def process_far(self, frame) -> None:
        """远端参考帧（播放线程）：缓冲 → 10ms 对齐 → APM 反向入口（无返回值）。"""
        data = np.ascontiguousarray(frame, dtype=np.int16).ravel().tobytes()
        frame_bytes = self._frame_samples * 2
        with self._lock:
            self._far_buf.extend(data)
            while len(self._far_buf) >= frame_bytes:
                frame10 = np.frombuffer(bytes(self._far_buf[:frame_bytes]), dtype=np.int16)
                del self._far_buf[:frame_bytes]
                self._reverse(frame10)

    def process_mic(self, frame):
        """近端采集帧 → 回声消除残差（int16 numpy，长度与输入一致）。"""
        data = np.ascontiguousarray(frame, dtype=np.int16).ravel().tobytes()
        frame_bytes = self._frame_samples * 2
        with self._lock:
            self._near_buf.extend(data)
            if self._set_delay is not None:
                self._set_delay(int(self._delay_ms))           # process 前置（plan 定案）
            while len(self._near_buf) >= frame_bytes:
                frame10 = np.frombuffer(bytes(self._near_buf[:frame_bytes]), dtype=np.int16)
                del self._near_buf[:frame_bytes]
                processed = self._process(frame10)
                if processed is None:
                    processed = frame10                        # 绑定原位修改语义的兜底
                if isinstance(processed, np.ndarray):
                    processed = processed.astype(np.int16, copy=False).ravel().tobytes()
                self._out_buf.extend(processed)
            out_len = min(len(data), len(self._out_buf))
            out = bytes(self._out_buf[:out_len])
            del self._out_buf[:out_len]
        result = np.frombuffer(out, dtype=np.int16).copy()
        if len(result) < len(data) // 2:
            # 残差不足（10ms 粒度差）以原样尾补齐——≤10ms 的粒度差，无碍 RMS 门
            tail = np.frombuffer(data, dtype=np.int16)[len(result):]
            result = np.concatenate([result, tail])
        return result

    def close(self) -> None:
        with self._lock:
            self._far_buf.clear()
            self._near_buf.clear()
            self._out_buf.clear()
            self._apm = None
