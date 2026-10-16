"""每角色一库的记忆存取（M+1.1）—— MemoryStore + 持久化写队列。

plan.md M+1 上下文铁律：所有 store/vector/dedup/summarizer 按 **db 路径参数化** 设计，禁止 character_id SQL 过滤与跨角色查询——角色隔离唯一手段是库文件粒度（character_id 期权列仅作 forensic 标记：建表带列、无索引、写入恒 NULL、接口无此参数）。

**持久化写队列（v1.9.3 借鉴①）**：后台评估等写库一律 enqueue → flush：入队即原子落盘（tmp+os.replace）到库旁 pending 文件；flush 逐条写库（INSERT OR IGNORE by queue_id），事务提交成功才出队；Store 初始化时重放 pending——进程崩溃最多丢「尚未入队」的内容，已入队未落库的状态写回（AI Native 铁律）重启后自动补上。

线程安全：单连接（check_same_thread=False）+ 一把 threading.Lock 串行化全部 SQL（WAL 允许多读，本模块以简单正确的串行化优先；「并发写无死锁」验收见 test_memory_store）。SQL 一律在调用点内联静态字面量 + 参数绑定（安全铁律），不做任何动态拼装。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from server.constants import APP_ROOT, DEFAULT_PACKAGE_ID, active_package_id

logger = logging.getLogger(__name__)

# ---------------- M-CHA.3 装配点（保持既有导出与语义不变） ----------------

MEMORY_DIR = APP_ROOT / "data" / "memory"
LEGACY_DB_PATH = APP_ROOT / "data" / "memory.db"

__all__ = ["MemoryStore", "memory_db_path", "migrate_legacy_memory_db", "ENTRY_KINDS", "MEMORY_DIR", "LEGACY_DB_PATH"]

ENTRY_KINDS = ("dialogue", "fact", "diary", "emotion_tone")
_QUEUE_HOOK_KIND = "hook"            # 队列条目的特殊 kind：落 hooks 表
KINDS_FOR_QUEUE = ENTRY_KINDS + (_QUEUE_HOOK_KIND,)

EMOTION_TONE_KEEP_DAYS = 14   # emotion_tone 保留期（[reflect] 回看窗 7 天的 2 倍裕度）
HOOK_STALE_DAYS = 7           # 未消费钩子的过期天数（超期自动消费，停扫停追问）


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def memory_db_path(settings: dict | None = None, *, characters_root: Path | None = None, memory_dir: Path | None = None) -> Path:
    """每角色一库唯一装配点：data/memory/{激活角色}.db（M-CHA.3）。

    角色判定与 resolver 同源（active_package_id）：激活包缺失回退 miyako 时库名随之是 miyako.db——**绝不**按请求参数以外的角色开第二把连接。settings/characters_root/memory_dir 三参均为测试注入缝（产品代码不传）。
    """
    root = memory_dir or MEMORY_DIR
    package_id = active_package_id(settings, characters_root=characters_root)
    return root / f"{package_id}.db"


def migrate_legacy_memory_db(memory_dir: Path | None = None, legacy_path: Path | None = None) -> bool:
    """旧单库首启迁移（幂等；Store.__init__ 在开库前调用）。

    触发条件（精确定义）：旧 ``data/memory.db`` 存在，且 ``data/memory/`` 内**无任何 \\*.db**（.gitkeep 等非 db 文件忽略不算占用）。迁移目标**恒为** miyako.db——旧库先于多角色存在，与当前激活哪个角色包无关（用户定案）。-wal/-shm 边车一并移动（WAL 未 checkpoint 的数据在边车里，漏移即丢数据）。返回是否发生了迁移。
    """
    target_dir = memory_dir or MEMORY_DIR
    legacy = legacy_path if legacy_path is not None else LEGACY_DB_PATH
    if not legacy.is_file():
        return False
    target_dir.mkdir(parents=True, exist_ok=True)
    if any(target_dir.glob("*.db")):
        logger.info("首启迁移跳过：%s 已有角色库（旧库 %s 保留不动，由用户处置）", target_dir, legacy)
        return False
    target = target_dir / f"{DEFAULT_PACKAGE_ID}.db"
    for suffix in ("", "-wal", "-shm"):
        side = legacy.with_name(legacy.name + suffix) if suffix else legacy
        if side.is_file():
            side.replace(target.with_name(target.name + suffix))
    logger.info("首启迁移：旧记忆库 %s → %s（零丢失移动）", legacy, target)
    return True


class MemoryStore:
    """每角色一库的记忆存取门面。db_path=None 时按激活角色解析（M-CHA.3 装配）。"""

    # 取读的宽松上限：单用户桌宠数据量极小；kinds 过滤在 Python 侧完成，换取「每个 execute 都是调用点内联静态字面量」的安全性质（杜绝动态 SQL 这一整个类）。
    _READ_CAP = 2000

    def __init__(self, db_path: Optional[Path] = None, *, pending_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else memory_db_path()
        # 交接三条②：开库前先跑首启迁移（幂等；旧库不存在时立即返回 False）
        migrate_legacy_memory_db()
        self.pending_path = Path(pending_path) if pending_path is not None else Path(os.fspath(self.db_path) + ".pending.json")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_housekeep = float("-inf")   # 防膨胀维护降频计时（-inf=首次 record_dialogue 必跑；monotonic 是开机相对值，0 在新开机机器上 <600s 会漏首跑）
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # M+3.5：设置窗（客户端进程）与服务端并发同库，写锁冲突短等而非立即 SQLITE_BUSY
            self._conn.execute("PRAGMA busy_timeout=2000")
            # character_id = 期权列（forensic 标记）：无索引、无过滤、写入恒 NULL——隔离只靠一库一角色，此列存在仅为未来排查留指纹位。
            self._conn.execute("CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, content TEXT NOT NULL, importance REAL NOT NULL DEFAULT 0.5, ts TEXT NOT NULL, queue_id TEXT UNIQUE, character_id TEXT)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_kind_id ON entries (kind, id)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS hooks (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, created_at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, character_id TEXT)")
            # M+2.1 relationship 表随角色库走（plan 形状：单行 affection/trust + 期权列）
            self._conn.execute("CREATE TABLE IF NOT EXISTS dimensions (id INTEGER PRIMARY KEY CHECK (id = 1), affection REAL NOT NULL, trust REAL NOT NULL, updated_at TEXT NOT NULL, character_id TEXT)")
            self._repair_dimensions_shape()
            self._conn.commit()
        self.replay_pending()   # 崩溃恢复：上次进程已入队未落库的状态写回

    def _repair_dimensions_shape(self) -> None:
        """M+1.1 期旧形状（dimension/value 两行制，无 affection 列）→ M+2.1 plan 形状。

        旧表从未有写入方（M+2.1 是唯一消费者，本修复先于其落地），直接重建为
        空表零数据损失；列缺失即判定为旧形状。
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(dimensions)")}
        if "affection" not in cols:
            self._conn.execute("DROP TABLE dimensions")
            self._conn.execute("CREATE TABLE IF NOT EXISTS dimensions (id INTEGER PRIMARY KEY CHECK (id = 1), affection REAL NOT NULL, trust REAL NOT NULL, updated_at TEXT NOT NULL, character_id TEXT)")

    # ---------------- 直写（主链/工具路径；后台评估请走队列） ----------------
    def add(self, kind: str, content: str, *, importance: float = 0.5, ts: Optional[str] = None) -> int:
        """直接写入一条记忆条目，返回 rowid。character_id 列恒 NULL（期权列）。"""
        if kind not in ENTRY_KINDS:
            raise ValueError(f"未知条目类型：{kind!r}（闭集 {ENTRY_KINDS}）")
        with self._lock, self._conn:
            cur = self._conn.execute("INSERT INTO entries (kind, content, importance, ts) VALUES (?, ?, ?, ?)", (kind, str(content), _clamp(importance, 0.0, 1.0), ts or _now_iso()))
            return int(cur.lastrowid)

    def entries(self, kinds: Optional[list[str]] = None, limit: int = 500) -> list[sqlite3.Row]:
        """按类型取条目（新→旧）；检索与后台模块的统一读取口。

        kinds 给定时分 kind 各查一条静态字面量 SQL（``WHERE kind = ?`` + 绑定
        参数，安全性质不变），合并按 id 降序截断——**SQL 侧过滤**取代旧版
        「读 2000 行 Python 侧过滤」：无裁剪的 fact 会挤占全表读窗，超限后
        diary/emotion_tone 恒进不了检索池（召回静默退化），且每 reply 两次
        全量读行也白花。idx_entries_kind_id 让每 kind 查询走索引。"""
        limit = int(limit)
        with self._lock:
            if not kinds:
                rows = self._conn.execute(
                    "SELECT id, kind, content, importance, ts FROM entries "
                    "ORDER BY id DESC LIMIT ?", (self._READ_CAP,)).fetchall()
            else:
                merged: list[sqlite3.Row] = []
                for kind in dict.fromkeys(str(k) for k in kinds):
                    merged.extend(self._conn.execute(
                        "SELECT id, kind, content, importance, ts FROM entries "
                        "WHERE kind = ? ORDER BY id DESC LIMIT ?",
                        (kind, self._READ_CAP)).fetchall())
                rows = sorted(merged, key=lambda r: r["id"], reverse=True)
        return rows[:limit]

    def facts_recent(self, days: float = 3.0) -> list[sqlite3.Row]:
        """「收集近 N 天 fact」唯一查询方法（M+1.4 dedup 与 M+1.5 summarizer 共用）。"""
        cutoff = _hours_ago_iso(days * 24.0)
        with self._lock:
            return self._conn.execute("SELECT id, content, importance, ts FROM entries WHERE kind = 'fact' AND ts >= ? ORDER BY id DESC", (cutoff,)).fetchall()

    def get_entry_by_id(self, entry_id: int) -> Optional[sqlite3.Row]:
        """按 id 精确取单条（绑定参数静态字面量）。

        记忆管理「查看全文/编辑」的数据面：entries() 的窗口语义（全表或按
        kind 的最近 N 条）与表格列出集天然有差——按窗口线性扫描找 id 会对
        「表里有、详情查不到」的条目静默失败。"""
        with self._lock:
            return self._conn.execute(
                "SELECT id, kind, content, importance, ts FROM entries WHERE id = ?",
                (int(entry_id),)).fetchone()

    def has_entries(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM entries LIMIT 1").fetchone()
        return row is not None

    # ---------------- hooks（M+1.3 open_question 落点） ----------------
    def add_hook(self, question: str, *, created_at: Optional[str] = None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("INSERT INTO hooks (question, created_at) VALUES (?, ?)", (str(question), created_at or _now_iso()))
            return int(cur.lastrowid)

    def pending_hooks(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT id, question, created_at FROM hooks WHERE consumed = 0 ORDER BY id DESC").fetchall()

    def mark_hook_consumed(self, hook_id: int) -> bool:
        """回扣/已追问的钩子标记消费（M+2.3：回扣后不再追问）。"""
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE hooks SET consumed = 1 WHERE id = ?", (int(hook_id),))
            return cur.rowcount > 0

    # ---------------- 关系维度（M+2.1，plan 形状单行表） ----------------
    def get_relationship(self) -> Optional[dict]:
        """当前 affection/trust（无行 = 尚未初始化，返回 None）。"""
        with self._lock:
            row = self._conn.execute("SELECT affection, trust FROM dimensions WHERE id = 1").fetchone()
        return {"affection": float(row["affection"]), "trust": float(row["trust"])} if row else None

    def set_relationship(self, affection: float, trust: float) -> None:
        """ Upsert 关系数值（M+2.1 增量写库的落点；行恒单行 id=1）。"""
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO dimensions (id, affection, trust, updated_at) VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET affection = excluded.affection, trust = excluded.trust, updated_at = excluded.updated_at", (_clamp(affection, 0.0, 100.0), _clamp(trust, 0.0, 100.0), _now_iso()))

    # ---------------- 持久化写队列（v1.9.3 借鉴①） ----------------
    def enqueue_write(self, *, kind: str, content: str, importance: float = 0.5, confidence: float = 1.0, source: str = "manual", created_at: Optional[str] = None) -> str:
        """入队一条状态写回（原子落盘，进程崩溃不丢）。返回 queue_id。kind 为四类条目或 "hook"（content 即问题文本）。"""
        if kind not in KINDS_FOR_QUEUE:
            raise ValueError(f"未知队列条目类型：{kind!r}（{KINDS_FOR_QUEUE}）")
        entry = {"queue_id": uuid.uuid4().hex, "kind": kind, "content": str(content), "importance": _clamp(importance, 0.0, 1.0), "confidence": _clamp(confidence, 0.0, 1.0), "source": str(source), "created_at": created_at or _now_iso()}
        with self._lock:
            queue = self._read_queue_unlocked()
            queue.append(entry)
            self._write_queue_unlocked(queue)
        return entry["queue_id"]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._read_queue_unlocked())

    def replay_pending(self) -> int:
        """启动重放（= flush 的别名语义；返回本次落库条数）。"""
        return self.flush_pending()

    def flush_pending(self) -> int:
        """队列 → 库：逐条 INSERT OR IGNORE（queue_id 幂等），提交成功才出队。

        崩溃恢复语义：写库提交后、出队前崩溃 → 重启重放时 queue_id 已存在，IGNORE 跳过并出队——既不丢也不重。
        """
        flushed = 0
        with self._lock:
            queue = self._read_queue_unlocked()
            if not queue:
                return 0
            remaining: list[dict] = []
            for entry in queue:
                try:
                    with self._conn:
                        if entry["kind"] == _QUEUE_HOOK_KIND:
                            self._conn.execute("INSERT INTO hooks (question, created_at) VALUES (?, ?)", (entry["content"], entry["created_at"]))
                        else:
                            self._conn.execute("INSERT OR IGNORE INTO entries (kind, content, importance, ts, queue_id) VALUES (?, ?, ?, ?, ?)", (entry["kind"], entry["content"], entry["importance"], entry["created_at"], entry["queue_id"]))
                    flushed += 1       # 事务提交成功才计数/出队
                except sqlite3.Error:
                    logger.warning("写队列条目落库失败，保留在队列：%s", entry.get("queue_id"), exc_info=True)
                    remaining.append(entry)
            if len(remaining) != len(queue):
                self._write_queue_unlocked(remaining)
        if flushed:
            logger.debug("写队列落库 %d 条（剩余 %d）", flushed, self.pending_count())
        return flushed

    # ---------------- 队列文件（原子读写，调用方须持锁） ----------------
    def _read_queue_unlocked(self) -> list[dict]:
        try:
            if not self.pending_path.is_file():
                return []
            data = json.loads(self.pending_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            logger.warning("pending 队列文件损坏，按空队列处理（原文件保留待查）", exc_info=True)
            return []

    def _write_queue_unlocked(self, queue: list[dict]) -> None:
        tmp = self.pending_path.with_name(self.pending_path.name + ".tmp")
        tmp.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.pending_path)   # 原子替换：崩溃不产生半截队列

    # ---------------- dedup 落点（M+1.4；全部参数绑定，无动态 SQL） ----------------
    def update_content(self, entry_id: int, content: str) -> bool:
        """按 id 改写条目内容（dedup 合并落点）。返回是否命中。"""
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE entries SET content = ? WHERE id = ?",
                                     (str(content), int(entry_id)))
            return cur.rowcount > 0

    def delete_entry(self, entry_id: int) -> bool:
        """按 id 整条删除（dedup 删除落点 / M+3.5 记忆管理页共用）。返回是否命中。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM entries WHERE id = ?", (int(entry_id),))
            return cur.rowcount > 0

    def reset_all(self) -> dict:
        """一键重置（M+3.5：关系页/记忆页共用，二次确认在 GUI 层）。

        「清除旧人格锚定」定案：清 entries+hooks+dimensions 三表数据，
        表结构保留（迁移/形状修复语义不变）。返回各表清除行数。
        """
        with self._lock, self._conn:
            return {
                "entries": self._conn.execute("DELETE FROM entries").rowcount,
                "hooks": self._conn.execute("DELETE FROM hooks").rowcount,
                "dimensions": self._conn.execute("DELETE FROM dimensions").rowcount,
            }

    # ---------------- 对话原文（M+3.5/查看历史：四类件套的最后一块） ----------------
    DIALOGUE_KEEP_ROUNDS = 500   # 对话原文滚动保留轮数（超出裁最旧；回放用途不进检索池）
    _HOUSEKEEP_MIN_INTERVAL_S = 600.0   # emotion_tone 裁剪/钩子过期的最短执行间隔

    def record_dialogue(self, user_text: Optional[str], reply: str, *,
                        trigger: str = "user", keep: int = DIALOGUE_KEEP_ROUNDS) -> None:
        """一轮对话落库（单条 dialogue 条目 = 一组，走写队列防崩溃丢失）。

        content 格式：user 轮为「[用户] {user}\\n[miyako] {reply}」；
        behavior/boot 轮的用户侧是内部触发提示词，不上文（只存「[{trigger}] {reply}」）。
        落库后按 keep 滚动裁剪最旧轮次。检索侧不召回 dialogue（vector.RETRIEVAL_KINDS）。
        """
        if user_text is not None:
            content = f"[用户] {user_text}\n[miyako] {reply}"
        else:
            content = f"[{trigger}] {reply}"
        self.enqueue_write(kind="dialogue", content=content, importance=0.5,
                           source="dialogue")
        self.flush_pending()
        self.prune_dialogue(keep)
        # 防膨胀维护降频：emotion_tone 裁剪/钩子过期每 turn 固定执行 → 至多
        # 10 分钟一次（无匹配行时是纯开销；数据增长尺度是天级，降频无风险）
        now = time.monotonic()
        if now - self._last_housekeep >= self._HOUSEKEEP_MIN_INTERVAL_S:
            self._last_housekeep = now
            self.prune_emotion_tone()
            self.expire_stale_hooks()

    def prune_dialogue(self, keep: int = DIALOGUE_KEEP_ROUNDS) -> int:
        """滚动裁剪：只保留最近 keep 组 dialogue。返回裁掉行数。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM entries WHERE kind = 'dialogue' AND id NOT IN "
                "(SELECT id FROM entries WHERE kind = 'dialogue' ORDER BY id DESC LIMIT ?)",
                (int(keep),))
            return cur.rowcount

    def prune_emotion_tone(self, keep_days: int = EMOTION_TONE_KEEP_DAYS) -> int:
        """按保留期裁剪 emotion_tone（每 reply 落 1 条、零裁剪=常驻库无界增长）。

        14 天 >> [reflect] 消费的 7 天回看窗（behavior._reflect_count），
        检索侧旧 tone 行本就被 recency 因子沉底——到期裁掉无感。返回裁掉行数。
        """
        cutoff = (datetime.now() - timedelta(days=int(keep_days))).isoformat(timespec="seconds")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM entries WHERE kind = 'emotion_tone' AND ts < ?", (cutoff,))
            return cur.rowcount

    def expire_stale_hooks(self, keep_days: int = HOOK_STALE_DAYS) -> int:
        """过期钩子自动消费：created_at 早于 keep_days 且未消费 → 置 consumed。

        pending_hooks 全表扫描发生在**每条用户消息**（回扣判定）与每 30s tick
        （_pick_signal）——永不回扣的钩子无限累积=扫描成本线性爬升；话题挂了
        一周还没回扣/没过闸，也不再值得追问。返回过期条数。"""
        cutoff = (datetime.now() - timedelta(days=int(keep_days))).isoformat(timespec="seconds")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE hooks SET consumed = 1 WHERE consumed = 0 AND created_at < ?",
                (cutoff,))
            return cur.rowcount

    def dialogue_recent(self, limit: int = 200) -> list:
        """最近 limit 组对话原文（新→旧；查看历史窗口的数据源）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT id, content, ts FROM entries WHERE kind = 'dialogue' "
                "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
