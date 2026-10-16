"""句级打字机气泡（M1.5）：40ms/字、最多保留 3 句、样式读 settings.yaml ui_theme 节。

结构：``BubbleTextBuffer`` 是纯逻辑（Qt 无关，确定性单测）；
``BubbleWidget`` 只做渲染与 QTimer 接线。样式缺失/非法 → 内置默认（fail-open）。
"""
from __future__ import annotations

import logging
from collections import deque
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget

from server.constants import load_settings

logger = logging.getLogger(__name__)

DEFAULT_THEME: dict = {
    "font_family": "Microsoft YaHei UI",
    "font_size": 14,
    "text_color": "#333333",
    "bg_color": "#FFF8F0",
    "border_color": "#F4A7B9",
    "border_radius": 12,
    "bg_opacity": 0.92,
    "typing_interval_ms": 40,   # plan M1.5 定案：40ms/字
    "padding_h": 10,
    "padding_v": 6,
}


def apply_scale(theme: dict, scale: float) -> dict:
    """把整体缩放系数应用到主题派生值（字号/内边距/圆角；纯函数，构造与运行时共用）。

    scale ≤0 或 1.0 时原样返回拷贝；字号下限 8（防缩成不可读）。
    """
    out = dict(theme)
    scale = float(scale)
    if scale > 0 and scale != 1.0:
        out["font_size"] = max(8, round(float(out["font_size"]) * scale))
        out["padding_h"] = round(float(out["padding_h"]) * scale)
        out["padding_v"] = round(float(out["padding_v"]) * scale)
        out["border_radius"] = round(float(out["border_radius"]) * scale)
    return out


def load_bubble_theme(path: Path | None = None) -> dict:
    """读 settings.yaml 的 ``ui_theme.bubble`` 节（2026-09 应用层配置合一）。

    ``scale`` 为整体缩放系数（乘字号/内边距/圆角）；缺失/非法 → 默认主题
    （fail-open，不阻断）；不做旧独立 ui_theme.yaml 的兼容读取（单一事实源）。
    读取统一走唯一入口 ``server.constants.load_settings``（2026-09-12 收敛）。
    """
    theme = dict(DEFAULT_THEME)
    try:
        data = load_settings(path)
        if path is not None and not data:
            logger.info("气泡主题配置不存在或为空，使用内置默认样式")
        bubble = dict((data.get("ui_theme") or {}).get("bubble") or {})
        scale = float(bubble.pop("scale", 1.0) or 1.0)
        for key in DEFAULT_THEME:
            if key in bubble:
                theme[key] = bubble[key]
        # 扩展键（非 DEFAULT_THEME 成员但被运行时消费）——2026-09-11 修复：
        # 此前被加载器丢弃，yaml 里配置的 max_sentences 从未真正生效
        for key in ("max_sentences", "width", "height"):
            if key in bubble:
                theme[key] = bubble[key]
        theme = apply_scale(theme, scale)
    except Exception:
        logger.warning("气泡主题读取失败，使用内置默认样式", exc_info=True)
    return theme


BUBBLE_MAX_TEXT_WIDTH = 520   # 正文折行上限（px）：超宽自动换行，防单行横铺全屏


def measure_bubble(text: str, theme: dict,
                   max_text_width: int = BUBBLE_MAX_TEXT_WIDTH,
                   fixed_width: int = 0, fixed_height: int = 0) -> tuple[int, int]:
    """正文 → 气泡窗口 (宽, 高)（含内边距；超上限自动折行）。

    fixed_width/fixed_height >0 时锁定该维度（用户手动尺寸，绝对像素，
    不受整体缩放影响）：宽度锁定同时是折行上限，高度锁定时文字垂直居中；
    0 = 按内容自适应。

    背景（用户 2026-09-11 实测）：BubbleWidget 从未覆写 sizeHint，
    QWidget.adjustSize() 在内容尺寸无效时退回**平台默认新建窗尺寸**——
    截图里的「无法调的大框」就是它；缩放系数只乘字号/内边距，框从不跟随。
    本函数让窗口尺寸真正由正文实测（字模宽度 × 行数 + 内边距）决定，
    缩放时整框随文字一起缩放。
    """
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QFont, QImage, QPainter

    ph, pv = int(theme["padding_h"]), int(theme["padding_v"])
    fixed_width = max(0, int(fixed_width))
    fixed_height = max(0, int(fixed_height))
    if not text:
        return (fixed_width or 2 * ph), (fixed_height or 2 * pv)

    wrap_limit = (fixed_width - 2 * ph) if fixed_width > 2 * ph else max_text_width
    top_flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                    | Qt.TextFlag.TextWordWrap)
    # 🔑 用 QPainter.boundingRect 计尺寸——Qt 文档定义它与 drawText 是同一套
    # 排版引擎（测出来的 = 画出来的）。此前的 QFontMetrics 估算 + 行数折算
    # 在中英混排时会数少折行行数 → 底部新字被裁（真机 2026-09-12 二次截图）。
    probe = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(probe)
    try:
        painter.setFont(QFont(str(theme["font_family"]), int(theme["font_size"])))
        natural = painter.boundingRect(QRect(0, 0, 100000, 100000),
                                       int(Qt.AlignmentFlag.AlignLeft
                                           | Qt.AlignmentFlag.AlignTop), text)
        if natural.width() <= wrap_limit and fixed_width <= 0:
            # 单行（\n 各行）都在上限内：按实测宽度收身（+2 抗锯齿安全边）
            w = natural.width() + 2 * ph
            h = natural.height() + 2 * pv + 2
        else:
            limit = (fixed_width - 2 * ph) if fixed_width > 2 * ph else wrap_limit
            wrapped = painter.boundingRect(QRect(0, 0, limit, 100000), top_flags,
                                           text)
            w = limit + 2 * ph
            h = wrapped.height() + 2 * pv + 2
    finally:
        painter.end()
    if fixed_width > 0:
        w = fixed_width
    if fixed_height > 0:
        h = fixed_height
    return w, h


class BubbleTextBuffer:
    """打字机文本缓冲（纯逻辑）。

    - add()：新句到达；未打完的旧句直接补全并上移，开始打新句；
    - tick()：打字机推进一个节拍（每个 tick 显示 1 个字）；
    - visible_text()：已补全句（≤ max_sentences-1 行）+ 当前句已显示部分，
      总句数恒 ≤ max_sentences（默认 3）。
    """

    def __init__(self, max_sentences: int = 3) -> None:
        if max_sentences < 1:
            raise ValueError("max_sentences 必须 >= 1")
        self.max_sentences = max_sentences
        self._completed: deque[str] = deque(maxlen=max_sentences - 1)
        self._current = ""
        self._revealed = 0

    def set_max_sentences(self, n: int) -> None:
        """运行时改同时保留句数（设置窗「界面」实时项，M+3.5/用户反馈）。

        保留最近的 n-1 条已完成句（n=1 单句模式：无已完成句，只显示当前句）。
        """
        n = max(1, int(n))
        self.max_sentences = n
        self._completed = deque(self._completed, maxlen=max(0, n - 1))

    def add(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if self._current:
            self._completed.append(self._current)  # 旧句补全上移；deque 自动丢最旧
        self._current = text
        self._revealed = 0

    def tick(self, chars: int = 1) -> None:
        """推进打字机。由 40ms QTimer 驱动；chars 默认 1（40ms/字）。"""
        if self._current and self._revealed < len(self._current):
            self._revealed = min(len(self._current), self._revealed + max(1, chars))

    def visible_text(self) -> str:
        lines = list(self._completed)
        if self._current:
            lines.append(self._current[: self._revealed])
        return "\n".join(lines)

    @property
    def is_typing(self) -> bool:
        return bool(self._current) and self._revealed < len(self._current)

    @property
    def sentence_count(self) -> int:
        return len(self._completed) + (1 if self._current else 0)

    def full_text(self) -> str:
        """完整文本（当前句含未打完部分）——气泡窗口尺寸的依据。

        打字机逐字显示期间框以整句全文为准（不随已显字符跳动），
        下一句到达时才按新内容重算。
        """
        lines = list(self._completed)
        if self._current:
            lines.append(self._current)
        return "\n".join(lines)

    def clear(self) -> None:
        self._completed.clear()
        self._current = ""
        self._revealed = 0


class BubbleWidget(QWidget):
    """气泡渲染：透明无框小窗，锚定在立绘旁；QTimer 驱动打字机。"""

    def __init__(self, theme: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent,
                         Qt.FramelessWindowHint | Qt.ToolTip | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.theme = theme or load_bubble_theme()
        self._base_theme = dict(self.theme)   # 未缩放基准（set_scale 运行时重算用）
        self._scale = 1.0                      # 当前整体缩放（set_font_size 重算用）
        self._fixed_size = (max(0, int(self.theme.get("width") or 0)),
                            max(0, int(self.theme.get("height") or 0)))
        # 最多同时保留几句台词（ui_theme.bubble.max_sentences；M1.5 定案默认 3，
        # 1=单句模式；过长台词自动换行，视觉行数可能多于句数）
        self.buffer = BubbleTextBuffer(
            max_sentences=max(1, int(self.theme.get("max_sentences", 3))))
        self.auto_show = True   # 托盘「隐藏气泡」置 False：收句但不弹窗（粘性隐藏）
        self.anchor: QPoint | None = None      # 宠物窗口左上角（全局坐标）
        self._offset: QPoint | None = None     # 相对宠物的偏移；None=默认自动放置
        self._pet_width = 0
        self._dragging = False
        self._drag_last: QPoint | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(int(self.theme["typing_interval_ms"]))
        self._timer.timeout.connect(self._on_tick)

    # ---- 锚定（跟随角色 + 用户拖动偏移） ----
    def set_scale(self, scale: float) -> None:
        """运行时整体缩放（设置窗「界面」组实时调整；M+3.5/用户反馈）。"""
        self._scale = max(0.0, float(scale))
        self._base_theme.setdefault("font_size", DEFAULT_THEME["font_size"])
        self._base_theme.setdefault("padding_h", DEFAULT_THEME["padding_h"])
        self._base_theme.setdefault("padding_v", DEFAULT_THEME["padding_v"])
        self._base_theme.setdefault("border_radius", DEFAULT_THEME["border_radius"])
        self.theme = apply_scale(self._base_theme, self._scale)
        self._relayout()
        self.update()

    def set_font_size(self, size: int) -> None:
        """运行时改气泡基础字号（独立于整体缩放——缩放系数另乘；实时项）。"""
        self._base_theme["font_size"] = max(8, int(size))
        self.theme = apply_scale(self._base_theme, self._scale)
        self._relayout()
        self.update()

    def set_fixed_width(self, width: int) -> None:
        """锁定气泡宽度（绝对 px，0=按内容自适应；实时项）。"""
        self._fixed_size = (max(0, int(width)), self._fixed_size[1])
        self._relayout()
        self.update()

    def set_fixed_height(self, height: int) -> None:
        """锁定气泡高度（绝对 px，0=按内容自适应；文字垂直居中；实时项）。"""
        self._fixed_size = (self._fixed_size[0], max(0, int(height)))
        self._relayout()
        self.update()

    def set_max_sentences(self, n: int) -> None:
        """运行时改同时保留句数（设置窗实时项）：缓冲截断 + 按新内容重排版。"""
        self.theme["max_sentences"] = max(1, int(n))
        self.buffer.set_max_sentences(n)
        self._relayout()
        self.update()

    def set_bg_opacity(self, opacity: float) -> None:
        """运行时改气泡背景不透明度（设置窗「界面」实时项；文字独立调节）。

        0 = 背景全透明（只剩文字浮在桌面上）；与整体缩放无关。
        """
        self.theme["bg_opacity"] = max(0.0, min(1.0, float(opacity)))
        self.update()

    def set_text_opacity(self, opacity: float) -> None:
        """运行时改文字不透明度（设置窗「界面」实时项；与背景/整体缩放无关）。

        下限 0.05 防止文字完全不可见。
        """
        self.theme["text_opacity"] = max(0.05, min(1.0, float(opacity)))
        self.update()

    def set_pet_width(self, width: int) -> None:
        """宠物立绘显示宽度（默认放置位=立绘右侧）。"""
        self._pet_width = max(0, int(width))
        self._reposition()

    def set_anchor(self, global_pos) -> None:
        """宠物左上角全局坐标（角色拖动时由 moved 信号驱动，气泡相对跟随）。"""
        self.anchor = QPoint(global_pos)
        self._reposition()

    def set_offset(self, offset) -> None:
        """用户拖动后的相对偏移（相对宠物左上角，位置记忆持久化恢复）。

        防御性接受 QPoint 或 (x, y) 序列——恢复链路曾因传 tuple 直接
        TypeError 崩掉整个启动（2026-09-11 真机 crash-1.log 定位）。
        """
        if isinstance(offset, QPoint):
            self._offset = QPoint(offset)
        else:
            x, y = offset
            self._offset = QPoint(int(x), int(y))
        self._reposition()

    def _default_offset(self) -> QPoint:
        return QPoint(self._pet_width + 12, -(self.height() + 8))

    def _reposition(self) -> None:
        if self.anchor is None:
            return
        offset = self._offset if self._offset is not None else self._default_offset()
        pos = self.anchor + offset
        # 钳回虚拟桌面：默认偏移向上开一个气泡高度，宠物贴顶/贴边摆放（拖拽
        # 与位置记忆都不设限）时气泡会被整个移出屏幕——show 了也看不见
        screen = self.screen()
        if screen is not None:
            virtual = screen.virtualGeometry()
            width = max(1, self.width())
            height = max(1, self.height())
            pos.setX(max(virtual.left(), min(pos.x(), virtual.right() - width)))
            pos.setY(max(virtual.top(), min(pos.y(), virtual.bottom() - height)))
        self.move(pos)

    def show_sentence(self, text: str) -> None:
        self.buffer.add(text)
        if not self._timer.isActive():
            self._timer.start()
        self._relayout()
        if self.auto_show:      # 托盘隐藏气泡时：文本照收（重新显示即见），不弹窗
            self.show()

    def _on_tick(self) -> None:
        self.buffer.tick()
        self.update()
        if not self.buffer.is_typing:
            self._timer.stop()  # 打完即停定时器（0 息耗），文本保留到下一句到达

    def _relayout(self) -> None:
        font = QFont(self.theme["font_family"], int(self.theme["font_size"]))
        self.setFont(font)
        # 窗口尺寸=正文实测（不再退平台默认窗——「大框」根因，见 measure_bubble）；
        # 用户锁定宽高（width/height>0）时按锁定值
        self.resize(*measure_bubble(self.buffer.full_text(), self.theme,
                                    fixed_width=self._fixed_size[0],
                                    fixed_height=self._fixed_size[1]))
        self._reposition()

    # ---- 气泡单独拖动（调整与角色的相对位置；松手记住偏移） ----
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_last = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging and self._drag_last is not None:
            gp = event.globalPosition().toPoint()
            self.move(self.pos() + gp - self._drag_last)
            self._drag_last = gp
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            if self.anchor is not None:
                self._offset = self.pos() - self.anchor
            event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802
        text = self.buffer.visible_text()
        if not text:
            return
        theme = self.theme
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor(theme["border_color"]))
            painter.setBrush(QColor(theme["bg_color"]))
            painter.setOpacity(float(theme["bg_opacity"]))
            radius = int(theme["border_radius"])
            painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), radius, radius)
            painter.setOpacity(float(theme.get("text_opacity", 1.0)))   # 文字独立透明度（实时项）
            painter.setPen(QColor(theme["text_color"]))
            font = QFont(theme["font_family"], int(theme["font_size"]))
            painter.setFont(font)
            # 🔑 必须带 TextWordWrap——与 measure_bubble 的测量标志同源：
            # 缺失时长句画出右边界被裁掉（真机：折行后的第二行永远不可见）
            flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                        | Qt.TextFlag.TextWordWrap)
            margin_h, margin_v = int(theme["padding_h"]), int(theme["padding_v"])
            painter.drawText(self.rect().adjusted(margin_h, margin_v,
                                                  -margin_h, -margin_v), flags, text)
