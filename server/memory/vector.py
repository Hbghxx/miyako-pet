"""三因子记忆检索（M+1.2）—— sqlite-vec 向量召回 + 衰减/重要度精排，带降级。

检索分（plan.md M+1.2 定版公式，实现与文本逐字对齐）：
    score = 0.55*sim + 0.25*exp(-hours/(72*(0.5+importance))) + 0.20*importance
时间项半衰随 importance 加权：importance∈[0,1] → 时间常数 36h~108h（plan v1.8 文本写作 36h~144h，以公式为准——PROGRESS 已记）。

**嵌入来源（复述定案）**：本地确定性字符 bigram hash 嵌入（纯 stdlib、零网络零模型、维度固定、余弦相似度）——嵌入函数经 embed_fn 注入缝独立，未来换真嵌入模型只改一处（原则 1：模型层可插拔）。降级与嵌入解耦：嵌入不依赖扩展。

**降级（§4.1「sqlite-vec 装载失败」行）**：enable_load_extension / sqlite_vec 加载 / vec0 建表任一失败 → 纯关键词模式（query bigram 命中率作 sim 代理），retrieve 接口签名不变。接口无 character_id 参数（隔离只靠一库一角色）。

**超时（§4.1「记忆检索超时>200ms」行）**：retrieve 以 asyncio.wait_for 包裹（阻塞实现在 to_thread 里，超时可真实切断）→ 超时返回空 context，无感知。

SQL 全部在 execute 调用点内联静态字面量 + 参数绑定（安全铁律，与 store.py 同一纪律）。
"""
from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import struct
import zlib
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)

EMBED_DIM = 64
VEC_TIMEOUT_S = 0.2            # §4.1：检索超时 200ms 返回空
COARSE_FACTOR = 4              # 粗召回倍数：4×top_k → 三因子精排截 top-k
_WEIGHT_SIM, _WEIGHT_DECAY, _WEIGHT_IMPORTANCE = 0.55, 0.25, 0.20
_DECAY_TAU_BASE_H = 72.0       # 时间常数 = 72*(0.5+importance) 小时
# 🔑 检索池白名单（v1.9.4 定案）：dialogue 是「回放用途」的原文存档，只供查看
# 历史窗口读取，绝不进召回候选——原文进池会稀释提炼事实的召回并诱发逐字复述。
RETRIEVAL_KINDS = ("fact", "diary", "emotion_tone")


def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """确定性字符 bigram hash 嵌入（纯 stdlib）：分桶计数 → L2 归一化。"""
    vec = [0.0] * dim
    clean = "".join(ch for ch in (text or "").lower() if not ch.isspace())
    for i in range(max(0, len(clean) - 1)):
        bigram = clean[i:i + 2]
        bucket = zlib.crc32(bigram.encode("utf-8")) % dim
        vec[bucket] += 1.0
    if len(clean) == 1:
        # 单字符：没有 bigram（此前恒零向量，检索全靠 decay/importance 兜底）
        bucket = zlib.crc32(clean.encode("utf-8")) % dim
        vec[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _serialize_vec(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        # 损坏/缺失时间戳按「远古」处理（decay→0）——绝不当成刚写入的新鲜
        # 条目（此前回 now() 会给损坏行打满时间分，反而置顶）
        return datetime.min


def keyword_sim(query: str, content: str) -> float:
    """关键词模式的 sim 代理：query bigram 在 content 中的命中率（0~1）。"""
    clean_q = "".join(ch for ch in (query or "").lower() if not ch.isspace())
    clean_c = "".join(ch for ch in (content or "").lower() if not ch.isspace())
    bigrams = {clean_q[i:i + 2] for i in range(max(0, len(clean_q) - 1))}
    if not bigrams and len(clean_q) == 1:
        bigrams = {clean_q}               # 单字查询：退化为单字命中（否则恒 0 分）
    if not bigrams:
        return 0.0
    hits = sum(1 for bg in bigrams if bg in clean_c)
    return hits / len(bigrams)


class MemoryRetriever:
    """三因子检索门面。store 提供条目；vec 模式下本模块维护自有索引连接（WAL 多读）。"""

    def __init__(self, store: MemoryStore, *, timeout_s: float = VEC_TIMEOUT_S,
                 embed_fn: Optional[Callable[[str], list[float]]] = None,
                 db_path: Optional[Path] = None) -> None:
        self.store = store
        self.dim = EMBED_DIM
        self.timeout_s = timeout_s
        self.embed_fn = embed_fn or embed
        self.mode = "vec"          # vec | keyword（初始化时定，运行期不变）
        self._conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(db_path if db_path is not None else store.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS entry_vec USING vec0(entry_id INTEGER PRIMARY KEY, embedding float[64] distance_metric=cosine)")
            self._conn = conn
        except Exception:
            self.mode = "keyword"
            logger.warning("sqlite-vec 不可用，记忆检索降级纯关键词（接口签名不变）", exc_info=True)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ---------------- 索引同步（vec 模式） ----------------
    def _sync_vec(self, rows: Optional[list] = None) -> None:
        """增量补嵌。``rows`` 传调用方已取的 entries 读（检索路径每轮共用一次
        全量读，免重复拉 2000 行）；None 时自行读（独立同步入口）。"""
        if rows is None:
            rows = self.store.entries(kinds=list(RETRIEVAL_KINDS),
                                      limit=self.store._READ_CAP)
        known = {r["entry_id"] for r in self._conn.execute("SELECT entry_id FROM entry_vec").fetchall()}
        for row in rows:
            if row["id"] in known:
                continue
            self._conn.execute("INSERT INTO entry_vec (entry_id, embedding) VALUES (?, ?)", (row["id"], _serialize_vec(self.embed_fn(row["content"]))))
        self._conn.commit()

    # ---------------- 检索 ----------------
    async def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """三因子检索；超时/任何失败一律空 context（§4.1 降级矩阵，调用方无感知）。

        注：to_thread 线程无法被 wait_for 真取消——超时后孤儿线程仍会把该次
        检索跑完（含 _sync_vec），完成后结果被丢弃。
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._retrieve_sync, query, top_k),
                timeout=self.timeout_s)
        except asyncio.TimeoutError:
            logger.debug("记忆检索超时（>%.0fms）→ 空 context", self.timeout_s * 1000)
            return []
        except Exception:
            logger.warning("记忆检索失败 → 空 context（§4.1 降级，不阻塞回复）",
                           exc_info=True)
            return []

    def _retrieve_sync(self, query: str, top_k: int) -> list[dict]:
        rows = self.store.entries(kinds=list(RETRIEVAL_KINDS),
                                  limit=self.store._READ_CAP)   # 每轮仅此一次全量读
        if self.mode == "vec":
            self._sync_vec(rows)
            coarse_k = max(1, int(top_k) * COARSE_FACTOR)
            hits = self._conn.execute("SELECT entry_id, distance FROM entry_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance", (_serialize_vec(self.embed_fn(query)), coarse_k)).fetchall()
            sim_by_id = {h["entry_id"]: max(0.0, 1.0 - float(h["distance"])) for h in hits}
            candidates = [(row, sim_by_id.get(row["id"], 0.0))
                          for row in rows
                          if row["id"] in sim_by_id]
        else:
            candidates = [(row, keyword_sim(query, row["content"]))
                          for row in rows]
        now = datetime.now()
        scored = []
        for row, sim in candidates:
            sc = self._score(sim, row["importance"], row["ts"], now)
            scored.append((sc, {"content": row["content"], "importance": row["importance"],
                                "ts": row["ts"], "sim": round(sim, 6), "score": round(sc, 6)}))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:max(1, int(top_k))]]

    def _score(self, sim: float, importance: float, ts: str, now: datetime) -> float:
        """三因子综合分（公式见模块 docstring；纯函数，test_memory_decay 直接对拍）。"""
        hours = max(0.0, (now - _parse_ts(ts)).total_seconds() / 3600.0)
        tau = _DECAY_TAU_BASE_H * (0.5 + max(0.0, min(1.0, float(importance))))
        decay = math.exp(-hours / tau)
        return (_WEIGHT_SIM * sim + _WEIGHT_DECAY * decay
                + _WEIGHT_IMPORTANCE * max(0.0, min(1.0, float(importance))))

    # ---------------- 索引失效（M+3.5 记忆管理：编辑/重置必须同步向量） ----------------
    def forget(self, entry_id: int) -> None:
        """单条索引失效：内容被改写后旧嵌入作废，下轮 _sync_vec 按新文本重嵌。"""
        if self._conn is None:
            return                                  # keyword 模式无索引（实时读库）
        self._conn.execute("DELETE FROM entry_vec WHERE entry_id = ?", (int(entry_id),))
        self._conn.commit()

    def clear_index(self) -> None:
        """整表索引失效（reset_all 配套；下轮 _sync_vec 按剩余条目重建=空）。"""
        if self._conn is None:
            return
        self._conn.execute("DELETE FROM entry_vec")
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
