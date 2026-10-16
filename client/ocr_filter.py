"""本地 OCR 过滤（M+3.2）—— RapidOCR lazy import，同 hash 缓存。

降级矩阵（§4.1「OCR 缺失/崩溃」行的客户端半边）：
  - extras 未装 / 引擎初始化失败 / 单帧崩溃 → ``run`` 返回 None；
    闲置嗅探据此**自动禁用**（screen_capture.sniffing 判据），
    手动热键不带 ocr 字段 → 服务端直接走 VLM 路。
  - 引擎 lazy：import 与初始化都推迟到首次 ``run``/``available``，
    OCR extra 依赖（rapidocr-onnxruntime/numpy）缺失不影响启动。

同 hash 缓存：同屏反复触发（热键连按/回座）不重复推理，缓存恒为
「最近一屏」一条（内存态，进程退出即弃）。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class OcrFilter:
    """[(text, conf)] 提取器。engine_factory 注入缝（测试替身；生产懒加载）。"""

    def __init__(self, engine_factory: Optional[Callable[[], object]] = None,
                 convert_fn: Optional[Callable[[object], object]] = None) -> None:
        self._engine_factory = engine_factory
        self._convert_fn = convert_fn or qimage_to_bgr   # 测试注入缝（哨兵图直通）
        self._engine: Optional[object] = None
        self._broken = False              # 初始化失败/未安装 → 永久降级（不反复重试）
        self._cache_hash: Optional[str] = None
        self._cache_result: Optional[list[tuple[str, float]]] = None

    def available(self) -> bool:
        """OCR 是否可用（首次调用时 lazy 初始化引擎；失败永久记降级）。"""
        if self._engine is not None:
            return True
        if self._broken:
            return False
        try:
            self._engine = (self._engine_factory or _load_rapidocr)()
        except Exception:
            logger.info("RapidOCR 不可用（extras 未装或初始化失败）→ "
                        "闲置嗅探自动禁用，热键走 VLM 路", exc_info=True)
            self._broken = True
            return False
        if self._engine is None:
            self._broken = True
            return False
        return True

    def run(self, image, hash_hex: Optional[str]) -> Optional[list[tuple[str, float]]]:
        """QImage → [(text, conf)]；不可用/失败/空结果 → None。

        同 hash 直接回缓存（引擎零调用）；hash 为 None 视为无法缓存，
        每次实跑。
        """
        if hash_hex and hash_hex == self._cache_hash:
            return self._cache_result
        if not self.available():
            return None
        try:
            raw = self._engine(self._convert_fn(image))
        except Exception:
            logger.warning("OCR 单帧崩溃（§4.1 降级：本帧无文本路）", exc_info=True)
            return None
        result = _parse_lines(raw)
        if hash_hex:
            self._cache_hash = hash_hex
            self._cache_result = result
        return result


def _load_rapidocr() -> object:
    """生产引擎装载（唯一 import 点；ocr extra：rapidocr-onnxruntime）。"""
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def qimage_to_bgr(image):
    """QImage → BGR ndarray（RapidOCR 输入惯例；numpy 为 ocr extra 传递依赖）。"""
    import numpy as np
    from PySide6.QtGui import QImage

    img = image.convertToFormat(QImage.Format.Format_RGB888)
    stride = img.bytesPerLine()
    arr = np.frombuffer(bytes(img.constBits()), dtype=np.uint8).reshape(
        img.height(), stride)[:, :img.width() * 3].reshape(
        img.height(), img.width(), 3)
    return np.ascontiguousarray(arr[:, :, ::-1])          # RGB → BGR


def _parse_lines(raw) -> Optional[list[tuple[str, float]]]:
    """RapidOCR 输出 → [(text, conf)]。

    形状兼容：rapidocr-onnxruntime ≥1.4 的 ``__call__`` 返回 ``(结果, 耗时)``
    二元组（先解包，``(None, None)`` → None）；旧形态平铺列表
    ``[[box, text, score], …]`` 直通。
    """
    if isinstance(raw, tuple) and len(raw) == 2:
        raw = raw[0]
    if not raw:
        return None
    lines: list[tuple[str, float]] = []
    for item in raw:
        try:
            text, conf = str(item[1]).strip(), float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if text:
            lines.append((text, conf))
    return lines or None
