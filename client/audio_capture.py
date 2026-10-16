"""麦克风采集（M1.6）。

链路：sounddevice 回调模式（16kHz / blocksize=512≈32ms / PCM16 / numpy）→
PyAudio 兜底 → 都不可用降级「禁用语音输入」+ 托盘提示（notify 注入，
M1.10 托盘接线前为日志）。帧经 on_frame 回调上抛（int16 numpy，形状 (512, 1)）；
原始 PCM 不跨进程边界（原则 9）。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCKSIZE = 512  # ≈32ms


class AudioCapture:
    """采集器：start()/stop() 生命周期 + available 状态。

    on_frame 回调在音频线程被调用（注意线程安全：消费侧自行投递到主线程）。
    """

    def __init__(self, on_frame: Optional[Callable[[np.ndarray], None]] = None,
                 notify: Optional[Callable[[str], None]] = None) -> None:
        self._on_frame = on_frame or (lambda frame: None)
        self._notify = notify or (lambda msg: logger.warning("%s", msg))
        self._stream = None
        self._pyaudio = None
        self.available = False
        self.backend = ""

    # ---- 生命周期 ----
    def start(self) -> bool:
        if self.available:
            return True
        if self._try_sounddevice():
            return True
        if self._try_pyaudio():
            return True
        self.available = False
        self._notify("语音输入不可用：已禁用语音输入，文本输入不受影响")
        return False

    def stop(self) -> None:
        try:
            if self._stream is not None:
                # stop 与 close 分开兜底：stop() 抛异常不得跳过 close（泄漏设备）
                try:
                    self._stream.stop()
                except Exception:
                    logger.debug("采集流停止异常", exc_info=True)
                try:
                    self._stream.close()
                except Exception:
                    logger.debug("采集流关闭异常", exc_info=True)
        finally:
            self._stream = None
            if self._pyaudio is not None:
                try:
                    self._pyaudio.terminate()
                except Exception:
                    logger.debug("PyAudio 终止异常", exc_info=True)
                self._pyaudio = None
            self.available = False
            self.backend = ""

    # ---- sounddevice（默认） ----
    def _try_sounddevice(self) -> bool:
        stream = None
        try:
            import sounddevice as sd

            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, blocksize=BLOCKSIZE,
                channels=1, dtype="int16", callback=self._sd_callback)
            stream.start()
        except Exception as exc:
            logger.warning("sounddevice 采集不可用，尝试 PyAudio 兜底：%s", exc)
            # start 失败时 InputStream 已持有设备侧资源，必须显式关（此前泄漏）
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logger.debug("采集流关闭异常", exc_info=True)
            return False
        self._stream = stream
        self.available = True
        self.backend = "sounddevice"
        logger.info("麦克风采集启动（sounddevice，16kHz/512/PCM16）")
        return True

    def _sd_callback(self, indata, frames, time_info, status) -> None:
        if status:
            logger.debug("采集状态：%s", status)
        self._on_frame(indata.copy())  # (512, 1) int16

    # ---- PyAudio（兜底） ----
    def _try_pyaudio(self) -> bool:
        pa = None
        stream = None
        try:
            import pyaudio  # 非打包依赖：仅作为 sounddevice 失败时的本机兜底

            pa = pyaudio.PyAudio()
            self._pyaudio = pa  # 先持有引用：回调启动后即需读取 paContinue
            stream = pa.open(
                format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                input=True, frames_per_buffer=BLOCKSIZE,
                stream_callback=self._pa_callback)
            stream.start_stream()
        except Exception as exc:
            logger.warning("PyAudio 兜底不可用：%s", exc)
            self._pyaudio = None
            # 失败路径显式释放：open 成功但 start_stream 失败 → 先关流；
            # PyAudio 实例已初始化 COM/WASAPI 侧 → terminate（清引用等 GC 不确定回收）
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logger.debug("采集流关闭异常", exc_info=True)
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    logger.debug("PyAudio 终止异常", exc_info=True)
            return False
        self._stream = stream
        self.available = True
        self.backend = "pyaudio"
        logger.info("麦克风采集启动（pyaudio 兜底，16kHz/512/PCM16）")
        return True

    def _pa_callback(self, in_data, frame_count, time_info, status):
        try:
            frame = np.frombuffer(in_data, dtype=np.int16).reshape(-1, 1).copy()
            self._on_frame(frame)
        except Exception:
            logger.exception("PyAudio 回调异常")
        continue_flag = getattr(self._pyaudio, "paContinue", 0) if self._pyaudio else 0
        return None, continue_flag
