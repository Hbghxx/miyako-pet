"""Speex AEC 实现（M+1.5.1 Windows 平替，v1.9.4 定案）—— pyaec（aec-rs 绑定）。

背景：webrtc-audio-processing 在 Windows 无轮子且停更（PyPI 全版本仅源码包 +
Linux ARMv7 旧轮子，C++ 源码构建在 Windows 失败，实测）；pyaec 提供
**win_amd64 官方轮子**的 Rust 版 Speex AEC，本机合成信号实测 **33.9dB 衰减**
（>10dB 验收线），定为 Windows 缺省 AEC 实现。

契约：pyaec ``Aec(frame_size, filter_length, sample_rate)`` +
``cancel_echo(rec, echo) → 残差``——**远端/近端必须成对**消费。因此：
process_far 只缓冲远端；process_mic 缓冲近端后按 frame_size 成对取用，
远端滞后时近端帧排队等待（对齐保证消除质量）。残差长度与输入一致
（不足部分以未处理近端尾补齐——远端未到帧至多延迟 ≤1 帧的消除）。

线程安全：process_far（播放线程）/process_mic（采集线程）并发，一把
threading.Lock 包住缓冲与 pyaec 调用（与 aec_webrtc 同一纪律）。
enable_preprocess=True 附带 Speex 降噪（残差更干净，利于 barge-in 门）。
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

from client.aec_base import AecBase

logger = logging.getLogger(__name__)


class AecSpeex(AecBase):
    """pyaec（aec-rs/Speex）AEC 封装。aec_factory 注入供测试缝。"""

    name = "speex"

    def __init__(self, config: Optional[dict] = None,
                 aec_factory: Optional[Callable[[], object]] = None) -> None:
        cfg = config or {}
        self._rate = int(cfg.get("sample_rate", 16000))
        self._frame_size = int(cfg.get("frame_size", 256))          # 16ms @16k
        self._filter_length = int(cfg.get("filter_length", 2048))   # 回声尾长（样本）
        self._lock = threading.Lock()
        self._far_buf = bytearray()
        self._near_buf = bytearray()
        self._out_buf = bytearray()
        if aec_factory is not None:
            self._aec = aec_factory(self._frame_size, self._filter_length, self._rate)
        else:
            import pyaec   # extras aec（Windows 官方 win_amd64 轮子）

            self._aec = pyaec.Aec(self._frame_size, self._filter_length, self._rate,
                                  enable_preprocess=True)

    def process_far(self, frame) -> None:
        """远端参考帧（播放线程）：仅缓冲，消费发生在 process_mic 的成对处理。"""
        data = np.ascontiguousarray(frame, dtype=np.int16).ravel().tobytes()
        with self._lock:
            self._far_buf.extend(data)

    def process_mic(self, frame):
        """近端采集帧 → 残差（int16 numpy，长度与输入一致）。"""
        data = np.ascontiguousarray(frame, dtype=np.int16).ravel().tobytes()
        with self._lock:
            if not self._far_buf:
                # 远端静默期（未在播放）：无参考信号可消——近端直通并清积压。
                # 此前积压会在下次播放时与新风成对消费：错位整个静默时长
                # （残留=分钟前的旧音频 → 消除失效/barge-in 死掉 + 无界内存）
                self._near_buf.clear()
                self._out_buf.clear()
                return np.frombuffer(data, dtype=np.int16).copy()
            self._near_buf.extend(data)
            frame_bytes = self._frame_size * 2
            while (len(self._near_buf) >= frame_bytes
                   and len(self._far_buf) >= frame_bytes):
                near = np.frombuffer(bytes(self._near_buf[:frame_bytes]), dtype=np.int16)
                echo = np.frombuffer(bytes(self._far_buf[:frame_bytes]), dtype=np.int16)
                del self._near_buf[:frame_bytes]
                del self._far_buf[:frame_bytes]
                out = self._aec.cancel_echo(near, echo)
                self._out_buf.extend(np.asarray(out, dtype=np.int16).tobytes())
            out_len = min(len(data), len(self._out_buf))
            out = bytes(self._out_buf[:out_len])
            del self._out_buf[:out_len]
        result = np.frombuffer(out, dtype=np.int16).copy()
        if len(result) < len(data) // 2:
            # 远端滞后帧的残差至多延迟 1 帧到达；不足部分以未处理近端尾补齐
            tail = np.frombuffer(data, dtype=np.int16)[len(result):]
            result = np.concatenate([result, tail])
        return result

    def close(self) -> None:
        with self._lock:
            self._far_buf.clear()
            self._near_buf.clear()
            self._out_buf.clear()
            self._aec = None
