"""AEC 抽象与工厂（M1.6）。

接口三件：process_far（远端参考=播放帧）/ process_mic（近端采集，返回处理后帧）/ close。
降级矩阵（§4.1）：AEC 缺失/初始化失败 → 直通 + 播放期全丢弃 + barge_in 强制 false +
托盘提示（notify 注入；M1.10 托盘接线前为日志）。

工厂返回 ``(aec, barge_in_enabled)``：webrtc 实装在 M+1.5（当前 aec_webrtc.py
仅有 TODO 占位，import 失败即自动降级 aec_none）。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class AecBase(ABC):
    """AEC 接口。帧为 int16 numpy 数组（16kHz 单声道）。"""

    name = "base"

    @abstractmethod
    def process_far(self, frame):
        """远端参考帧（播放侧）喂入；供回声消除建立参考。"""

    @abstractmethod
    def process_mic(self, frame):
        """近端采集帧处理，返回消除回声后的帧。"""

    def set_stream_delay(self, delay_ms: float) -> None:
        """播放端 latency 更新（M+1.5.1：process_mic 前置调用）。

        默认无操作——webrtc APM 需要显式延迟，Speex 类自适应延迟自估。
        契约层统一入口，接线无需区分实现。
        """

    @abstractmethod
    def close(self) -> None:
        """释放底层资源。"""



class AecNone(AecBase):
    """直通实现：输出 == 输入（零拷贝）。"""

    name = "none"

    def process_far(self, frame):
        return frame

    def process_mic(self, frame):
        return frame

    def close(self) -> None:
        pass


def create_aec(config: dict | None, notify=None) -> tuple[AecBase, bool]:
    """按 settings.audio 段选择 AEC 实现。

    返回 (aec 实例, barge_in 是否可用)。语义：
      - audio.aec == "speex" 且初始化成功 → (AecSpeex, barge_in 配置值)
        （v1.9.4 定案：**Windows 缺省 AEC**——pyaec 有官方 win_amd64 轮子，
        合成信号实测 33.9dB；webrtc-audio-processing 在 Windows 无轮子且停更）；
      - audio.aec == "webrtc" 且初始化成功 → (AecWebrtc, barge_in 配置值)
        （Linux/macOS 可装 extras 时的选项）；
      - 初始化失败（extras 缺失等）→ (AecNone, False) + notify 托盘提示；
      - audio.aec 其它值（none 等）    → (AecNone, False)；
        若配置里 barge_in=true 同样强制关闭并提示（无 AEC 参考无法区分人声）。
    """
    notify = notify or (lambda msg: logger.warning(msg))
    config = config or {}
    requested = str(config.get("aec", "none")).lower()
    barge_in_requested = bool(config.get("barge_in", False))

    if requested in ("webrtc", "speex"):
        try:
            if requested == "speex":
                from client.aec_speex import AecSpeex  # lazy（extras aec；Windows 缺省）

                return AecSpeex(config), barge_in_requested
            from client.aec_webrtc import AecWebrtc  # lazy（extras aec）

            return AecWebrtc(config), barge_in_requested
        except Exception as exc:
            logger.warning("AEC %s 初始化失败，降级直通：%s", requested, exc)
            notify(f"AEC {requested} 不可用（extras aec 未安装或初始化失败），"
                   "已降级为直通并关闭 barge-in；语音识别照常可用")
            return AecNone(), False

    if barge_in_requested:
        notify("audio.aec 未启用 AEC 实现：barge-in 强制关闭（无 AEC 参考无法区分人声）")
    return AecNone(), False
