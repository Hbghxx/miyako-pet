"""会话级（一次启动→关闭）token 统计（2026-09-17 用户需求）。

llm_api 的 per_role 注册表与 usage_store 持久化都是「累计」视角；本模块补
「本次使用」视角：以**轮**（一次 ReplyBegin→ReplyEnd，orchestrator 漏斗）为
单位，按 llm_api 总 token 的轮首/轮末差值计量，记录 总量 / 单轮最高 / 单轮最低，
并按来源分桶（user=对话 / behavior=主动搭话 / screen=识屏 / companion=陪玩）。

纯内存态：server 重启即清零——重启天然就是「每次使用（启动-关闭）」的边界。
线程安全（后台槽可能跨线程触达）；snapshot 返回深拷贝防外部篡改。
"""
from __future__ import annotations

import datetime as dt
import threading


class SessionTokenStats:
    """会话级 token 聚合。record_turn 由 orchestrator 轮末调用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset_unlocked()

    def _reset_unlocked(self) -> None:
        self._started_at = dt.datetime.now().isoformat(timespec="seconds")
        self._turns = 0
        self._total = 0
        self._max = 0
        self._min: int | None = None            # None = 尚无已记录轮次
        self._vlm_tokens = 0                    # VLM 画面理解（非轮次消耗）
        self._sources: dict[str, dict] = {}

    def record_turn(self, total_tokens: int, source: str) -> None:
        """记一轮：total_tokens=本轮 llm_api 总 token 差值，source=触发来源。"""
        total = max(0, int(total_tokens))
        with self._lock:
            self._turns += 1
            self._total += total
            self._max = max(self._max, total)
            if self._min is None or total < self._min:
                self._min = total
            bucket = self._sources.setdefault(source, {
                "turns": 0, "total_tokens": 0, "vlm_tokens": 0,
                "max_turn_tokens": 0, "min_turn_tokens": None})
            bucket["turns"] = bucket.get("turns", 0) + 1
            bucket["total_tokens"] = bucket.get("total_tokens", 0) + total
            bucket["max_turn_tokens"] = max(bucket.get("max_turn_tokens", 0),
                                            total)
            prev_min = bucket.get("min_turn_tokens")
            if prev_min is None or total < prev_min:
                bucket["min_turn_tokens"] = total

    def record_extra(self, tokens: int, source: str) -> None:
        """非轮次消耗（VLM 画面理解等）：计入总量与来源桶的 vlm_tokens。

        不进轮次 turns/max/min——轮 = reply 漏斗，口径不变；来源桶与轮次
        共用（陪玩桶 = 轮次 reply 消耗 + 陪玩画面理解消耗，即真实总账）。
        """
        tokens = max(0, int(tokens))
        with self._lock:
            self._total += tokens
            self._vlm_tokens += tokens
            bucket = self._sources.setdefault(source, {
                "turns": 0, "total_tokens": 0, "vlm_tokens": 0,
                "max_turn_tokens": 0, "min_turn_tokens": None})
            bucket["total_tokens"] = bucket.get("total_tokens", 0) + tokens
            bucket["vlm_tokens"] = bucket.get("vlm_tokens", 0) + tokens

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self._started_at,
                "turns": self._turns,
                "total_tokens": self._total,
                "max_turn_tokens": self._max,
                "min_turn_tokens": self._min,
                "vlm_tokens": self._vlm_tokens,
                "sources": {name: dict(bucket)
                            for name, bucket in self._sources.items()},
            }
