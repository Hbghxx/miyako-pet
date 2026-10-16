"""能量 VAD 断句状态机（M-VOICE/VOICE.1，阶段一零依赖兜底层）。

纯逻辑、无 Qt/无音频依赖：调用方逐帧喂 RMS（喂时可注入 now——测试确定性），
状态机返回事件：
  ""      继续（无状态迁移）
  "start" 检测到说话开始（预滚帧由调用方自行管理）
  "end"   说完（静默 ≥tail_ms 且总时长 ≥min_speech_ms；或达 max_ms 强制截断）
  "drop"  说完了但总时长 <min_speech_ms——按噪声丢弃（调用方不应上行）

参数（settings voice.vad）：threshold / tail_ms / min_speech_ms / max_ms，
以及尾点自适应（延迟批次①c，2026-09）：tail_adaptive / tail_max_ms。
Silero-ONNX 主选升级路径见 plan M-VOICE（本类是能量兜底层，接口对齐）。

尾点自适应（tail_adaptive，默认开）：固定 tail_ms 是「误切 vs 每轮干等」的
折中，自适应用 VAD 自身可见的信号自愈——
  · 静默型 end 之后 resume 窗（1.5s）内再开口 = 切早了 → 尾点阶梯上浮
    （+150ms/次，上限 tail_max_ms）；
  · 安静越过恢复窗 = 尾点偏大 → 缓慢回落（-100ms/次），**下限恒为配置的
    tail_ms**（用户配置值是地板，自适应只上浮、回落不破配置意图）；
  · max_ms 强制截断与 drop（噪声）不参与学习——前者没发生静默、后者是
    阈值问题不是尾点问题。
on_adapt(ms) 回调供调用方出诊断日志（None=静默）。
"""
from __future__ import annotations

import time
from typing import Callable, Optional


class UtteranceVAD:
    IDLE = "idle"
    SPEAKING = "speaking"

    _RESUME_WINDOW_S = 1.5     # end 后多久内再开口算「被切早了」
    _STEP_UP_S = 0.15          # 切早一次的上浮步长
    _STEP_DOWN_S = 0.10        # 安静一次的回落步长

    def __init__(self, *, threshold: float = 0.03,
                 tail_ms: int = 700, min_speech_ms: int = 250,
                 max_ms: int = 15000,
                 tail_adaptive: bool = True, tail_max_ms: int = 1500,
                 now: Optional[Callable[[], float]] = None,
                 on_adapt: Optional[Callable[[int], None]] = None) -> None:
        self.threshold = max(0.001, float(threshold))
        self.tail_s = max(0.05, tail_ms / 1000.0)
        self.tail_floor_s = self.tail_s     # 配置值=地板：自适应只在 [floor, max] 浮动
        self.tail_max_s = max(self.tail_floor_s, tail_max_ms / 1000.0)
        self.tail_adaptive = bool(tail_adaptive)
        self.min_speech_s = max(0.0, min_speech_ms / 1000.0)
        self.max_s = max(0.5, max_ms / 1000.0)
        self._now = now or time.monotonic
        self.on_adapt = on_adapt
        self.state = self.IDLE
        self._speak_started = 0.0
        self._last_voice = 0.0
        self._last_end: Optional[float] = None   # 待评估的静默型 end（None=无）

    @property
    def speaking(self) -> bool:
        return self.state == self.SPEAKING

    @property
    def effective_tail_ms(self) -> int:
        return int(round(self.tail_s * 1000))

    def reset(self) -> None:
        self.state = self.IDLE
        self._last_end = None

    def _adapt(self, delta_s: float) -> None:
        new_s = min(self.tail_max_s, max(self.tail_floor_s,
                                         self.tail_s + delta_s))
        if new_s != self.tail_s:
            self.tail_s = new_s
            if self.on_adapt is not None:
                self.on_adapt(self.effective_tail_ms)

    def _settle_pending(self, now: float, voice: bool) -> None:
        """IDLE 态结算上一句遗留的尾点评估（voice=本帧是否有声）。"""
        if self._last_end is None:
            return
        gap = now - self._last_end
        if voice:
            if gap <= self._RESUME_WINDOW_S:
                self._adapt(self._STEP_UP_S)      # 切早了：上浮
            self._last_end = None                 # 慢开口不奖不罚，评估作废
        elif gap > self._RESUME_WINDOW_S:
            self._adapt(-self._STEP_DOWN_S)       # 安静越过恢复窗：回落并结算
            self._last_end = None
        # 安静且未越窗：保留待评估，继续观察（end 后的尾静默帧不该冲掉评估）

    def feed(self, rms: float) -> str:
        """喂一帧 RMS（0~1），返回 ""/"start"/"end"/"drop"。"""
        now = self._now()
        voice = rms >= self.threshold

        if self.state == self.IDLE:
            self._settle_pending(now, voice)
            if voice:
                self.state = self.SPEAKING
                self._speak_started = now
                self._last_voice = now
                return "start"
            return ""

        # SPEAKING
        if voice:
            self._last_voice = now
        total = now - self._speak_started
        if total >= self.max_s:
            self.state = self.IDLE
            return "end"                      # 强制截断（上限兜底；不参与尾点学习）
        if now - self._last_voice >= self.tail_s:
            self.state = self.IDLE
            # min_speech 度量「说话跨度」（last_voice 起点），尾静默不计入
            if self._last_voice - self._speak_started >= self.min_speech_s:
                if self.tail_adaptive:
                    self._last_end = now      # 静默型 end：留下次 IDLE 帧评估尾点
                return "end"
            return "drop"                     # 过短：按噪声丢弃（不上行、不学习）
        return ""
