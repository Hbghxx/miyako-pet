"""关系维度（M+2.1）—— emotion 槽合并调用增量写库 + 分支 tone_hint 注入。

数据流：orchestrator._emotion_merge 的评估结果（affection/trust 增量 -3~3）→
``Relationship.apply_emotion()`` 增量累加进 dimensions 单行表（随角色库走，
M-CHA.3 装配）→ orchestrator 经 RelState{affection,trust} 下行通知客户端
（v1.8 改名，勿复用 state）。

分支映射：.char 包 relationship.yaml 四分支（0-20/20-50/50-80/80+ 各带
tone_hint），按当前 affection 首中即停；tone_hint 由 orchestrator 注入
persona 段（Section 树 tone_hint 节）。任何失败静默跳过（关系不变）——
降级矩阵「关系评估失败」行。

character_id 期权列纪律同 entries：建列、无索引、无过滤、写入恒 NULL。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from server.constants import resolve_character_package
from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_DELTA_LIMIT = 3.0        # 单轮增量上限（plan：-3~3）
_VALUE_LIMIT = 100.0      # 维度取值域 0~100


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


class Relationship:
    """关系维度门面。store 提供单行表；branches 从激活包 relationship.yaml 读取。"""

    def __init__(self, store: MemoryStore, *, branches_path: Optional[Path] = None) -> None:
        self.store = store
        self.branches_path = (branches_path if branches_path is not None
                              else resolve_character_package() / "relationship.yaml")
        self._branches: Optional[list[dict]] = None   # lazy 加载，失败不重试刷屏
        self._branches_loaded = False
        # dimensions.min/max/per_reply_delta 接入消费（v1.9.6，life 卡可自定义
        # 关系节奏）；缺失/非法 → 内置缺省（initial 已由 _initial_values 消费）
        self._limits = {"affection": (0.0, _VALUE_LIMIT, -_DELTA_LIMIT, _DELTA_LIMIT),
                        "trust": (0.0, _VALUE_LIMIT, -_DELTA_LIMIT, _DELTA_LIMIT)}
        self._load_dimension_limits()

    def _load_dimension_limits(self) -> None:
        """读取 dimensions.affection/trust 的 min/max/per_reply_delta 为钳制边界
        （per-dimension）。文件缺失/解析失败 → 保持内置缺省（fail-open）。"""
        try:
            if self.branches_path is None or not self.branches_path.is_file():
                return
            import yaml

            data = yaml.safe_load(self.branches_path.read_text(encoding="utf-8")) or {}
            dims = data.get("dimensions") if isinstance(data, dict) else None
            if not isinstance(dims, dict):
                return
            for name in self._limits:
                d = dims.get(name)
                if not isinstance(d, dict):
                    continue
                vmin, vmax = d.get("min"), d.get("max")
                if vmin is not None and vmax is not None:
                    lo, hi = sorted((float(vmin), float(vmax)))
                    _, _, dlo, dhi = self._limits[name]
                    self._limits[name] = (lo, hi, dlo, dhi)
                delta = d.get("per_reply_delta")
                if isinstance(delta, (list, tuple)) and len(delta) == 2:
                    dlo, dhi = sorted((float(delta[0]), float(delta[1])))
                    vmin2, vmax2, _, _ = self._limits[name]
                    self._limits[name] = (vmin2, vmax2, dlo, dhi)
        except Exception:
            logger.debug("dimensions 钳制边界读取失败，用内置缺省", exc_info=True)

    # ---------------- 增量写库（M+2.1 主入口） ----------------
    def apply_emotion(self, affection_delta: float, trust_delta: float) -> Optional[dict]:
        """评估增量 → 累加进 dimensions；返回新值供 RelState 下行；失败返回 None。"""
        try:
            current = self.store.get_relationship()
            if current is None:
                initial = self._initial_values()
                current = current or initial
            amin, amax, alo, ahi = self._limits["affection"]
            tmin, tmax, tlo, thi = self._limits["trust"]
            new_a = max(amin, min(amax, current["affection"] + max(alo, min(ahi, float(affection_delta)))))
            new_t = max(tmin, min(tmax, current["trust"] + max(tlo, min(thi, float(trust_delta)))))
            self.store.set_relationship(new_a, new_t)
            return {"affection": round(new_a, 4), "trust": round(new_t, 4)}
        except Exception:
            logger.warning("关系增量写库失败（静默跳过，关系不变）", exc_info=True)
            return None

    # ---------------- 分支 tone_hint（persona 注入源） ----------------
    def branch_tone_hint(self) -> Optional[str]:
        """按当前 affection 首中分支（0-20/20-50/50-80/80+），返回 tone_hint。"""
        branches = self._load_branches()
        if not branches:
            return None
        current = self.store.get_relationship()
        if current is None:
            return None
        affection = current["affection"]
        for branch in branches:
            if float(branch.get("min", 0)) <= affection <= float(branch.get("max", 100)):
                hint = str(branch.get("tone_hint") or "").strip()
                return hint or None
        return None

    def _load_branches(self) -> Optional[list[dict]]:
        if self._branches_loaded:
            return self._branches
        self._branches_loaded = True
        try:
            import yaml

            if not self.branches_path.is_file():
                logger.debug("relationship.yaml 不存在：%s（分支映射停用）", self.branches_path)
                return None
            data = yaml.safe_load(self.branches_path.read_text(encoding="utf-8"))
            branches = data.get("branches") if isinstance(data, dict) else None
            if isinstance(branches, list) and branches:
                self._branches = branches
        except Exception:
            logger.warning("relationship.yaml 读取失败（分支映射停用）", exc_info=True)
        return self._branches

    def _initial_values(self) -> dict:
        """relationship.yaml dimensions.initial（缺省 10/10，与 miyako 卡一致）。"""
        initial = {"affection": 10.0, "trust": 10.0}
        try:
            import yaml

            if self.branches_path.is_file():
                data = yaml.safe_load(self.branches_path.read_text(encoding="utf-8"))
                dims = (data or {}).get("dimensions") if isinstance(data, dict) else None
                if isinstance(dims, dict):
                    for name in ("affection", "trust"):
                        value = (dims.get(name) or {}).get("initial")
                        if value is not None:
                            value = float(value)
                            lo, hi = self._limits[name][0], self._limits[name][1]
                            initial[name] = max(lo, min(hi, value))   # initial 也落 [min,max] 区间
        except Exception:
            logger.debug("initial 值读取失败，用缺省 10/10", exc_info=True)
        return initial
