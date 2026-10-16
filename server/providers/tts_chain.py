"""TTS 四级链 reply 级锁定（M2.4）。

探活链：①gptsovits（TCP 连 9880，与 preflight 同语义）→ ②edge（真实 dry-run，
结果缓存 5 分钟防每 reply 联网）→ ③audiofile（本地文件免探活，M+1.6 落位前
不在链上）→ ④mock（免探活，链尾兜底）。

铁律（§0.4/§4.2）：reply 开始 ``lock()`` 探活一次、**句间不切换**——音色撕裂
比音质下降更破坏人格一致性；reply 中途失败由 orchestrator 调 ``degrade()``
当场降级重试当前句（不再探活，直接构造下一级）。降级经 notify 上报（托盘提示）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class TtsLevel:
    name: str
    build: Callable[[], object]  # lazy 构造 provider（探活通过才实例化）
    probe: Optional[Callable[[], Awaitable[bool]]] = None  # None=免探活（永远可用）
    probe_cache_ttl: float = 0.0  # >0：探活结果缓存秒数（edge=300）


class TtsChain:
    """有序 provider 级链：探活锁定 + 中途降级。构建一次，每 reply 调 ``lock()``。"""

    def __init__(self, levels: list[TtsLevel],
                 notify: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not levels:
            raise ValueError("TtsChain 需要至少一级")
        self._levels = list(levels)
        self._notify = notify or (lambda msg: logger.warning("%s", msg))
        self._clock = clock
        self._index: Optional[int] = None
        self.active = None
        self._probe_cache: dict[str, tuple[float, bool]] = {}
        self._built: dict[int, object] = {}   # 级 → 已构建实例（复用，见 _instance）

    # ---------------- reply 开始：探活锁定 ----------------
    async def lock(self):
        """自顶向下探活，锁定首个可用级；锁定后句间不再切换。"""
        for i, level in enumerate(self._levels):
            if await self._probe_ok(level):
                self._index = i
                self.active = self._instance(i)
                if i > 0:
                    self._notify(f"TTS 降级：{self._levels[0].name} 探活失败，"
                                 f"本 reply 锁定 {level.name}")
                return self.active
        # 防御（末级 mock 免探活，正常不可达）
        self._index = len(self._levels) - 1
        self.active = self._instance(self._index)
        return self.active

    # ---------------- reply 中途：当场降级 ----------------
    def degrade(self):
        """当前级失败 → 下一级不再探活直接构造锁定；已到末级返回 None（纯气泡）。"""
        if self._index is None or self._index + 1 >= len(self._levels):
            return None
        self._index += 1
        level = self._levels[self._index]
        self.active = self._instance(self._index)
        self._notify(f"TTS 中途失败，当场降级 → {level.name}")
        return self.active

    def _instance(self, index: int):
        """按级复用实例：provider 懒建的 httpx.AsyncClient 从不 aclose，每 reply
        重建=连接池累积泄漏；复用还让流内解析的真实采样率跨 reply 保留
        （否则每 reply 首句恒报标称值，非标采样率输出会整句变调）。"""
        if index not in self._built:
            self._built[index] = self._levels[index].build()
        return self._built[index]

    @property
    def active_name(self) -> str:
        return self._levels[self._index].name if self._index is not None else ""

    # ---------------- 内部 ----------------
    async def _probe_ok(self, level: TtsLevel) -> bool:
        if level.probe is None:
            return True
        if level.probe_cache_ttl > 0:
            cached = self._probe_cache.get(level.name)
            if cached is not None and self._clock() - cached[0] < level.probe_cache_ttl:
                return cached[1]
        try:
            ok = bool(await level.probe())
        except Exception:
            logger.warning("TTS %s 探活异常，按不可用处理", level.name, exc_info=True)
            ok = False
        if level.probe_cache_ttl > 0:
            self._probe_cache[level.name] = (self._clock(), ok)
        return ok

    def level_names(self) -> list[str]:
        return [level.name for level in self._levels]
