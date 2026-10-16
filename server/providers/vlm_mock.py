"""Mock VLM（M1.7）：固定描述，供识屏链路 mock 跑通。"""
from __future__ import annotations

from server.providers.vlm_base import VlmBase

MOCK_DESCRIPTION = "屏幕上显示着一个游戏画面，有对话框和倒计时。"


class MockVlm(VlmBase):
    name = "mock"

    async def describe(self, image_b64: str, text_hint: str | None = None,
                       source: str = "vision") -> str:
        return MOCK_DESCRIPTION
