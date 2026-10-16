"""双档打断检测（M+1.5.2）+ 系统音频防御（M+1.5.3）—— 纯逻辑，时钟可注入。

双档（plan 定版表值）：
  AEC 档：残差 >0.03 持续 400ms（AEC 已消自身播放声，播放期也可触发）
  直通档：仅非播放期，>0.06 持续 600ms
实机阈值 = max(表值, 2×rms_floor)：进入语音模式先采集 3s 环境噪声基线
（v1.8），无样本/标定失败退表值。触发后需回落到阈值下再武装（防连续误发）。

回声泄漏门（真机②轮实测驱动，AEC 档播放期追加）：残差还须超过
**0.5×远端播放能量（far RMS）** 才认作他人语音。Speex AEC 对真实
扬声器→麦克风回路的残差泄漏（没消干净的自家人声）会超过绝对表值——
否则每次 TTS 一出声就被自打断 flush 掉，表现为「只有气泡没语音」。
远端静音（far=0）时该门自动失效，退回绝对阈值。

系统音频防御（M+1.5.3）：播放期系统混音峰值减去自身输出贡献仍显著 → 他在放
音频 → 静音门；**peak 不可用（-1）同样退静音门**（保守，降级矩阵行）；
粘滞：进入防御后需外部峰值回落到阈值一半以下才解除（防抖动）。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_ECHO_GATE_RATIO = 0.5        # 回声泄漏门：播放期残差须 > 0.5×far RMS 才算他人语音


class FarPeakTracker:
    """远端播放「峰值」追踪——与 pycaw 系统混音峰值同刻度（峰值 vs 峰值）。

    背景（2026-09-11 真机①不触发复盘）：防御门原先拿 far **RMS**（均方根
    刻度）去减系统**峰值**——语音的峰值天然比 RMS 高 10dB 以上，播放期
    external 恒超阈值 → 永久静音门 → barge-in 检测器被饿死（②安静零误触、
    ③音乐防御均 pass，唯独①被误杀，与症状完全吻合）。改为喂「近期远端块
    峰值的滑动最大」后两者可比：自身独播时 external≈0 不误静音，他人叠加
    音频时系统峰值显著高于自身峰值 → 照常进防御门。
    """

    def __init__(self, window: int = 8) -> None:
        self._peaks: deque[float] = deque(maxlen=max(1, int(window)))

    def feed(self, pcm16) -> float:
        """喂一块 int16 PCM（playback.far_frame tee 的另一路），返回当前值。"""
        peak = 0.0
        try:
            if pcm16 is not None and len(pcm16):
                import numpy as np

                peak = float(np.abs(np.asarray(pcm16)).max()) / 32768.0
        except Exception:
            logger.debug("远端峰值计算失败（本块按 0 计）", exc_info=True)
        self._peaks.append(peak)
        return self.value

    @property
    def value(self) -> float:
        """滑动窗内峰值（0~1；无样本=0）。"""
        return max(self._peaks) if self._peaks else 0.0


class BargeInDetector:
    """双档打断检测器。feed 每麦克风帧调一次（残差 RMS），返回是否触发打断。"""

    def __init__(self, *, aec_mode: bool = True,
                 threshold_aec: float = 0.03, threshold_passthrough: float = 0.06,
                 sustain_aec_s: float = 0.4, sustain_passthrough_s: float = 0.6,
                 baseline_s: float = 3.0,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._aec_mode = aec_mode
        self._table_threshold = threshold_aec if aec_mode else threshold_passthrough
        self._sustain_s = sustain_aec_s if aec_mode else sustain_passthrough_s
        self._baseline_s = baseline_s
        self._baseline_until = self._clock() + baseline_s
        self._baseline_samples: list[float] = []
        self._rms_floor: Optional[float] = None
        self._exceed_since: Optional[float] = None
        self._armed = True
        self.last_gate: Optional[float] = None   # 最近一帧的生效门限（含回声泄漏门）

    @property
    def threshold(self) -> float:
        """实机阈值 = max(表值, 2×rms_floor)；未标定/标定失败 → 表值（v1.8）。"""
        if self._rms_floor is None:
            return self._table_threshold
        return max(self._table_threshold, 2.0 * self._rms_floor)

    _BASELINE_MIN_SAMPLES = 50   # 窗口关闭的样本下限（3s 或 50 样本先到为准，防浮点累加窗口永不闭合）

    def restart_baseline(self) -> None:
        """重新开 3s 噪声基线采集窗（进入语音模式时调用，plan v1.8 定案时机）。

        清空旧 floor/样本与超阈状态——阈值随当下环境（含 AEC 残差水平）自适应。
        """
        self._baseline_until = self._clock() + self._baseline_s
        self._baseline_samples = []
        self._rms_floor = None
        self._exceed_since = None
        self._armed = True

    def feed(self, rms: float, playing: bool,
             far_rms: Optional[float] = None) -> bool:
        """喂入当前帧能量；满足持续时长条件返回 True（每轮至多触发一次）。

        far_rms：当前远端播放能量（RMS，playback.energy）——AEC 档播放期
        启用回声泄漏门（见模块 docstring）；缺省/0 时退回绝对阈值。
        """
        now = self._clock()
        if self._rms_floor is None and len(self._baseline_samples) < self._BASELINE_MIN_SAMPLES \
                and now < self._baseline_until:
            self._baseline_samples.append(float(rms))
            return False                                        # 噪声基线采集窗（3s）
        if self._rms_floor is None:
            if self._baseline_samples:                          # 窗口关闭：立即计算 floor
                self._rms_floor = sum(self._baseline_samples) / len(self._baseline_samples)
                logger.info("噪声基线标定完成：rms_floor=%.4f → 阈值=%.4f",
                            self._rms_floor, self.threshold)
            else:
                logger.info("噪声基线无样本（标定失败）→ 使用表值 %.3f", self._table_threshold)
        if not self._aec_mode and playing:
            self._exceed_since = None                        # 直通档播放期不检测
            self._armed = True
            return False
        threshold = self.threshold
        if self._aec_mode and playing and far_rms is not None and far_rms > 0:
            threshold = max(threshold, _ECHO_GATE_RATIO * float(far_rms))
        self.last_gate = threshold                           # 触发日志/诊断读这里
        if rms > threshold:
            if self._exceed_since is None:
                self._exceed_since = now
            if self._armed and now - self._exceed_since >= self._sustain_s - 1e-9:
                self._armed = False                          # 触发后需回落再武装
                return True
        else:
            self._exceed_since = None
            self._armed = True
        return False


class SystemAudioDefense:
    """播放期系统音频防御（M+1.5.3）：他人放音频 → 播放期全丢弃静音门。"""

    def __init__(self, *, threshold: float = 0.05, clock: Optional[Callable[[], float]] = None) -> None:
        self._threshold = threshold
        self._clock = clock or time.monotonic
        self._muted_since: Optional[float] = None

    def should_mute(self, peak: float, own_level: float, playing: bool) -> bool:
        """播放期防御判定。**peak 与 own_level 必须同刻度（均为峰值）**——
        own_level 传 FarPeakTracker.value（远端播放峰值）；传 RMS 会因
        峰值/RMS 天然差被误判「他人在放音频」（真机①复盘，见类 docstring）。

        peak<0（不可用）→ 静音门（保守强制）；外部显著 → 静音门；
        粘滞：防御中需外部峰值回落到阈值一半以下才解除。
        """
        if not playing:
            self._muted_since = None
            return False                                     # 非播放期无防御面
        now = self._clock()
        if peak < 0:                                         # 不可用 → 保守静音门
            if self._muted_since is None:
                self._muted_since = now
            return True
        external = peak - min(peak, max(0.0, own_level))     # 减自身贡献（不为负）
        if self._muted_since is not None:
            if external <= self._threshold / 2.0:
                self._muted_since = None                     # 回落过半才解除（防抖）
                return False
            return True
        if external > self._threshold:
            self._muted_since = now
            return True
        return False
