"""StagePresenter（M1.4）—— 表现层统一接口（原则 4：UI 与 AI 解耦）。

五方法（set_emotion / set_talking / play_motion / show_bubble / idle）是
表现层的唯一入口：客户端逻辑只认接口，不摸 sprite_manager 内部。

SpritePresenter 是 sprite_manager 的**语义收敛包装**而非转发泄漏——
set_emotion 校验 9 元闭集后才转发（与契约层脏行语义一致，共享 emotion_defs）、
set_talking 收敛为布尔、play_motion 受 manifest 约束。
若把 manager 的内部状态机方法原样暴露成公共接口，约束 B（§0.3）形同虚设。

import 约束 B（§0.3）：client/ 下除 presenter.py 与 sprite_manager.py 外，
禁止 import sprite_manager —— test_stage_presenter.py AST 执法。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional, Protocol

from client.sprite_manager import SpriteManager
from server.emotion_defs import DEFAULT_EMOTION, SENTIMENT_CLOSE_SET

logger = logging.getLogger(__name__)


class StagePresenter(Protocol):
    """表现层五方法契约。"""

    def set_emotion(self, emotion: str) -> None:
        """设置情绪（闭集外的值按 normal 处理，与契约层一致）。"""

    def set_talking(self, talking: bool) -> None:
        """口型状态开关（音频链路驱动）。"""

    def play_motion(self, motion: str) -> None:
        """播放命名 motion；受 manifest 约束，未登记的 motion 静默忽略。"""

    def show_bubble(self, text: str) -> None:
        """气泡显示一句台词（M1.5 打字机气泡接线下，降级为日志）。"""

    def idle(self) -> None:
        """回到待机演出。"""


class SpritePresenter:
    """SpriteManager 的五方法收敛包装。"""

    name = "sprite"

    def __init__(self, manager: SpriteManager,
                 on_bubble: Optional[Callable[[str], None]] = None) -> None:
        self._manager = manager
        self._on_bubble = on_bubble  # M1.5 BubbleWidget 的接线点；None 时降级为日志

    def set_emotion(self, emotion: str) -> None:
        if emotion not in SENTIMENT_CLOSE_SET:
            # 与契约层脏行语义一致：未知表情 → normal（同一 emotion_defs 闭集）
            logger.warning("presenter: 未知情绪 %r → normal", emotion)
            emotion = DEFAULT_EMOTION
        self._manager.set_emotion(emotion)

    def set_talking(self, talking: bool) -> None:
        """口型状态开关（音频链路驱动）。

        M-SPRITE：口型帧素材不存在（从未显示过），说话态不再有独立演出；
        保留本方法维持表现层接口稳定，将来补口型素材时在此接入。
        """

    def play_motion(self, motion: str) -> None:
        """受 manifest 约束的闸门：motions 段未登记的 motion 直接忽略。

        M+2.4 实装：登记且配了 frames 的 motion 触发 sprite_manager EMOTE
        演出（120ms 轮换）；未配 frames 的登记名无视觉（不闪现）。
        """
        self._manager.trigger_motion(motion)

    def show_bubble(self, text: str) -> None:
        if self._on_bubble is None:
            logger.info("presenter bubble: %s", text)  # M1.5 接线前的降级路径
            return
        self._on_bubble(text)

    def idle(self) -> None:
        self._manager.on_activity()
