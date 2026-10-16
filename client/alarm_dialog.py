"""闹钟管理对话框（M-TOOL）：托盘「闹钟管理…」入口。

列表 = Toolbox.alarm_entries()（即 settings tools.alarms 的内存态）；增删经
Toolbox.add/remove_alarm → AlarmStore 写回 settings（行级保注释，flow 列表）。
格式："HH:MM"=每日重复 / "YYYY-MM-DD HH:MM"=一次性；非法条目在状态行提示。
调用方须持对话框引用保活（临时对象被 GC 即闪退——history_window 同款教训）。
"""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPushButton, QVBoxLayout)


class AlarmDialog(QDialog):
    def __init__(self, *, toolbox, parent=None) -> None:
        super().__init__(parent)
        self._toolbox = toolbox
        self.setWindowTitle("闹钟管理")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "格式：\"07:30\" = 每日重复；\"2026-10-06 07:30\" = 一次性\n"
            "（一次性触发后自动失效；启动时自动清理已过期条目）"))
        self._list = QListWidget(self)
        layout.addWidget(self._list)

        row = QHBoxLayout()
        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText("07:30 或 2026-10-06 07:30")
        row.addWidget(self._edit)
        add_btn = QPushButton("添加", self)
        add_btn.clicked.connect(self._on_add)
        row.addWidget(add_btn)
        layout.addLayout(row)

        del_btn = QPushButton("删除所选", self)
        del_btn.clicked.connect(self._on_remove)
        layout.addWidget(del_btn)

        self._status = QLabel("", self)
        layout.addWidget(self._status)

        close_btn = QPushButton("关闭", self)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)
        self._refresh()

    def _refresh(self) -> None:
        self._list.clear()
        for entry in self._toolbox.alarm_entries():
            self._list.addItem(entry)

    def _on_add(self) -> None:
        text = self._edit.text().strip()
        if not text:
            return
        if self._toolbox.add_alarm(text):
            self._edit.clear()
            self._status.setText("已添加。")
        else:
            self._status.setText(f"格式无效或已存在：{text}")
        self._refresh()

    def _on_remove(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        self._toolbox.remove_alarm(item.text())
        self._status.setText("")
        self._refresh()
