"""查看历史对话窗口（M+3.5/用户需求：galgame 式回放，数据源 = memory.db dialogue 条目）。

只读设计：一轮 = 一条 dialogue 条目（store.record_dialogue 写入的
「[用户] …\n[miyako] …」或「[trigger] …」组），窗口按时间正序渲染、
打开时自动滚到最新。刷新/关闭即可，不做编辑（改写走记忆管理页/重置）。

连接生命周期：懒创建 MemoryAdmin（激活角色判定与服务端同源），closeEvent
时释放（客户端进程内多窗口/多实例不泄漏连接）。
"""
from __future__ import annotations

import logging
from html import escape
from typing import Callable

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QPushButton, QTextBrowser,
                               QVBoxLayout)

logger = logging.getLogger(__name__)


def render_rounds(rounds: list[dict]) -> str:
    """dialogue_rounds() 结果 → HTML（旧→新正序，纯文本转义；纯函数可测）。"""
    blocks: list[str] = []
    for r in reversed(rounds):                        # 数据新→旧，渲染旧→新
        user = escape(r.get("user") or "") or "（主动搭话）"
        reply = escape(r.get("reply") or "")
        ts = escape(str(r.get("ts") or ""))
        blocks.append(
            f"<p style='color:#8a8a8a'><small>{ts}</small></p>"
            f"<p><b>你：</b>{user.replace(chr(10), '<br>')}</p>"
            f"<p><b>miyako：</b>{reply.replace(chr(10), '<br>')}</p><hr>")
    return "\n".join(blocks) if blocks else "<p>还没有对话记录。</p>"


class HistoryWindow(QDialog):
    """查看历史对话（只读）。admin_factory：() -> MemoryAdmin（run_client 注入）。"""

    def __init__(self, *, admin_factory: Callable[[], object], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("查看历史对话")
        self.resize(560, 520)
        self._admin_factory = admin_factory
        self._admin_obj = None

        layout = QVBoxLayout(self)
        self._browser = QTextBrowser(self)
        self._browser.setOpenExternalLinks(False)
        layout.addWidget(self._browser)
        row = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        row.addWidget(refresh_btn)
        row.addWidget(close_btn)
        layout.addLayout(row)
        self.refresh()

    def _admin(self):
        if self._admin_obj is None:
            self._admin_obj = self._admin_factory()
        return self._admin_obj

    def refresh(self) -> None:
        try:
            rounds = self._admin().dialogue_rounds()
        except Exception:
            logger.warning("对话历史读取失败", exc_info=True)
            rounds = []
        self._browser.setHtml(render_rounds(rounds))
        self._browser.verticalScrollBar().setValue(
            self._browser.verticalScrollBar().maximum())   # 滚到最新

    def closeEvent(self, event) -> None:               # noqa: N802
        try:
            if self._admin_obj is not None:
                self._admin_obj.close()
                self._admin_obj = None
        except Exception:
            logger.debug("历史窗口关闭清理失败（忽略）", exc_info=True)
        super().closeEvent(event)
