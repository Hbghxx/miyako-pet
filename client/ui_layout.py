"""UI 三项（立绘/气泡/输入框）位置的退出保存与启动恢复（用户 2026-09-11 需求）。

存储：settings.yaml ``ui_theme`` 节内六个键（pet_window.pos_x/pos_y、
bubble.offset_x/offset_y、input_bar.pos_x/pos_y）——遵守 v1.9.1 单文件配置
铁律，不新增独立状态文件；键缺失自动创建（行级保注释写入器与参数页同一纪律）。

语义：
  - 立绘/输入框存**屏幕绝对坐标**；气泡存**相对立绘锚点的偏移**（气泡跟随
    立绘，恢复偏移即恢复用户自定义布局）；
  - 恢复前做屏幕有效性检查（存档坐标不在任何屏幕内 → 忽略，回默认位），
    防换显示器/改分辨率后窗口落到不可见区域；
  - 保存时机 = aboutToQuit（托盘完全退出、切换角色重启共用 quit_all→quit）；
    崩溃/强杀不落盘（可接受的取舍：退出保存，不做每拖一次写一次盘的 IO）。

本模块保持 Qt 轻依赖（收发 tuple，QPoint 转换在调用侧），便于纯 IO 测试。
"""
from __future__ import annotations

import logging
from pathlib import Path

from client.settings_window import (_read_yaml_path,  # 写入/读取纪律单一来源
                                    _rewrite_key_at_path, _yaml_scalar)

logger = logging.getLogger(__name__)

_PET_X = ("ui_theme", "pet_window", "pos_x")
_PET_Y = ("ui_theme", "pet_window", "pos_y")
_BUB_X = ("ui_theme", "bubble", "offset_x")
_BUB_Y = ("ui_theme", "bubble", "offset_y")
_BAR_X = ("ui_theme", "input_bar", "pos_x")
_BAR_Y = ("ui_theme", "input_bar", "pos_y")


def save_ui_positions(pet: tuple[int, int] | None,
                      bubble_offset: tuple[int, int] | None,
                      bar: tuple[int, int] | None,
                      settings_path: Path | None = None) -> bool:
    """退出前落盘三项位置；任一 None 跳过该项。返回是否有写入。"""
    wrote = False
    for keys, value in ((_PET_X, pet[0] if pet else None),
                        (_PET_Y, pet[1] if pet else None),
                        (_BUB_X, bubble_offset[0] if bubble_offset else None),
                        (_BUB_Y, bubble_offset[1] if bubble_offset else None),
                        (_BAR_X, bar[0] if bar else None),
                        (_BAR_Y, bar[1] if bar else None)):
        if value is None:
            continue
        _rewrite_key_at_path(settings_path, keys, _yaml_scalar(int(value)))
        wrote = True
    if wrote:
        logger.info("UI 位置已保存（立绘/气泡偏移/输入栏）")
    return wrote


def load_ui_positions(settings_path: Path | None = None) -> dict[str, tuple[int, int] | None]:
    """启动时读取三项位置；缺键 → 该项 None（调用侧回默认位）。"""
    def _pair(x_keys, y_keys) -> tuple[int, int] | None:
        x = _read_yaml_path(settings_path, x_keys, None)
        y = _read_yaml_path(settings_path, y_keys, None)
        if x is None or y is None:
            return None
        try:
            return int(x), int(y)
        except (TypeError, ValueError):
            return None

    return {"pet": _pair(_PET_X, _PET_Y),
            "bubble_offset": _pair(_BUB_X, _BUB_Y),
            "bar": _pair(_BAR_X, _BAR_Y)}


def point_on_screen(x: int, y: int) -> bool:
    """坐标是否落在任一屏幕的可用区域内（换屏/改分辨率防丢）。Qt 缺席 → True。"""
    try:
        from PySide6.QtGui import QGuiApplication

        for screen in QGuiApplication.screens():
            if screen.availableGeometry().contains(x, y):
                return True
        return False
    except Exception:
        return True
