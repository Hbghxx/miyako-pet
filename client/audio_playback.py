"""TTS 流式播放（M1.6 阻塞写 → 2026-09 写线程解耦）。

契约（plan M1.6 + 演进）：
  - 每帧写设备前：经 Qt signal ``far_frame`` 发出本帧（tee——AEC 远端参考，M+1.5）；
  - 写后：按 stream latency 发 ``stream_delay``（毫秒，AEC set_stream_delay 更新）；
  - 计算本帧 RMS 发 ``energy``（口型开合 / barge-in 能量源）。

**写设备在独立写线程**（2026-09 实测修复）：主线程阻塞写在真实长音频序列下会
周期性冻结 GUI（设备缓冲满即阻塞）——表现为「语音到下一句了，气泡打字机/上移
还停着」。GUI 线程只入队（满时丢最旧保实时）；``flush()`` 清排队帧 + abort 设备
缓冲（interrupt 乐观打断即停）；``ensure_sample_rate()`` 跨 reply 采样率重建。
帧为 int16 numpy（ravel 后一维）。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


def rms_of(pcm16: np.ndarray) -> float:
    """PCM16 块的 RMS，归一化到 0~1；空块返回 0.0。"""
    if pcm16 is None:
        return 0.0
    flat = np.asarray(pcm16).ravel()
    if flat.size == 0:
        return 0.0
    x = flat.astype(np.float64) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


class AudioPlayback(QObject):
    far_frame = Signal(object)    # int16 numpy 块（tee：AEC 远端参考预留）
    stream_delay = Signal(float)  # 毫秒（write 后按 stream latency 更新）
    energy = Signal(float)        # RMS 0~1

    QUEUE_SIZE = 256              # 排队帧上限（满丢最旧，保实时性）

    def __init__(self, sample_rate: int = 16000,
                 stream_factory: Optional[Callable[[], object]] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.sample_rate = sample_rate
        self._stream_factory = stream_factory or self._default_stream_factory
        self._stream = None                 # 仅写线程触碰
        self._stream_rate: Optional[int] = None   # 当前打开流的真实速率（仅写线程触碰）
        self._opened = False                # 写线程视角：流是否已打开（含打开失败）
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._writer: Optional[threading.Thread] = None
        self._running = False

    def _default_stream_factory(self):
        import sounddevice as sd

        return sd.OutputStream(samplerate=self.sample_rate, channels=1, dtype="int16")

    # ---------------- 生命周期（主线程 API） ----------------
    def start(self) -> bool:
        """启动写线程；设备流由写线程在首个写/重建命令时惰性打开。"""
        if self._writer is None or not self._writer.is_alive():
            self._running = True
            self._writer = threading.Thread(target=self._writer_loop,
                                            name="miyako-audio-writer",
                                            daemon=True)
            self._writer.start()
        return True

    def write(self, pcm16: np.ndarray) -> None:
        """入队一帧（非阻塞，绝不阻塞 GUI 线程）；tee → 写线程落设备。"""
        if not self._running:
            logger.debug("播放线程未启动，丢弃 %s 字节帧", getattr(pcm16, "nbytes", 0))
            return
        frame = np.ascontiguousarray(pcm16, dtype=np.int16).ravel()
        self.far_frame.emit(frame)                     # tee（AEC 预留）
        self._enqueue(("write", frame))

    def flush(self) -> None:
        """interrupt 即停：清掉排队帧，写线程 abort 设备缓冲（立即静音）。"""
        self._purge()
        self._enqueue(("flush", None))

    def ensure_sample_rate(self, sample_rate: int) -> bool:
        """跨 reply 采样率重建：变化 → 清队+重建流返回 True；一致返回 False。

        AudioBegin 携带每 reply 的真实采样率（①级 32k/②级 24k/mock 16k），
        播放流按到达值重建——「到达即播」下采样率错配会变调，必须重建。
        """
        if not self._running:
            self.start()
        if self._opened and sample_rate == self.sample_rate:
            return False
        self.sample_rate = sample_rate
        self._purge()
        self._enqueue(("rebuild", sample_rate))
        return True

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """等写线程清空队列（测试/关停辅助）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def stop(self) -> None:
        """停写线程并关流。写线程可能正阻塞在设备 write（缓冲满的常态）：
        先 abort 解堵（PortAudio abort 允许跨线程调用），流由写线程在退出前
        自行关闭——调用方线程直接 close 会与写线程的 write 并发（未定义行为）。"""
        self._running = False
        self._enqueue(("quit", None))
        self._abort_stream()
        if self._writer is not None:
            self._writer.join(timeout=2.0)
            if not self._writer.is_alive():
                self._writer = None
                self._close_stream()   # 幂等兜底（写线程 quit 分支已关）
            else:
                logger.warning("播放写线程 2s 未退出（卡在设备 write），流交由写线程收尾")

    # ---------------- 内部 ----------------
    def _enqueue(self, item: tuple) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:                       # 满时丢最旧腾位：音频帧保实时，控制命令
                self._queue.get_nowait()   # （quit/flush/rebuild）此前会被静默
                self._queue.task_done()    # 丢弃——长队列下 stop() 永远停不下来
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass

    def _purge(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def _open_stream(self) -> None:
        try:
            stream = self._stream_factory()
            if hasattr(stream, "start"):
                stream.start()
            self._stream = stream
            self._stream_rate = self.sample_rate
            logger.info("播放流启动（%dHz）", self.sample_rate)
        except Exception as exc:
            logger.warning("播放流打开失败（TTS 将降级为纯气泡）：%s", exc)
            self._stream = None
        finally:
            self._opened = True            # 失败也置位：不反复重试刷日志

    def _close_stream(self) -> None:
        try:
            if self._stream is not None and hasattr(self._stream, "close"):
                self._stream.close()
        except Exception:
            logger.debug("播放流关闭异常", exc_info=True)
        finally:
            self._stream = None
            self._stream_rate = None

    def _abort_stream(self) -> None:
        try:
            if self._stream is not None and hasattr(self._stream, "abort"):
                self._stream.abort()
        except Exception:
            logger.debug("flush（abort）失败", exc_info=True)

    # ---------------- 写线程 ----------------
    def _writer_loop(self) -> None:
        while True:
            kind, payload = self._queue.get()
            try:
                if kind == "quit":
                    self._close_stream()   # 流只在写线程内关（防跨线程并发 close/write）
                    return
                if kind == "flush":
                    self._abort_stream()
                    continue
                if kind == "rebuild":
                    self._close_stream()
                    self._open_stream()
                    continue
                # kind == "write"
                if self._stream is None:
                    self._open_stream()
                elif self._stream_rate != self.sample_rate:
                    # 速率漂移自愈：rebuild 命令可能被丢最旧逐出/被 flush 的
                    # purge 清掉，而 self.sample_rate 已是新值——写前核对，
                    # 不一致即重建（否则旧率流播新率数据=持续性变调无恢复）
                    self._close_stream()
                    self._open_stream()
                if self._stream is None:
                    continue               # 设备打不开 → 丢帧降级（纯气泡兜底）
                try:
                    self._stream.write(payload)
                    latency = float(getattr(self._stream, "latency", 0.0) or 0.0)
                    self.stream_delay.emit(latency * 1000.0)
                    self.energy.emit(rms_of(payload))
                except Exception:
                    logger.debug("音频帧写入失败，丢帧", exc_info=True)
            finally:
                self._queue.task_done()
