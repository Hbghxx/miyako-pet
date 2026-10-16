"""记忆管理门面（M+3.5 设置窗后端）—— 列表 / 删除 / 编辑 fact / 重置。

plan M+3.5 验收三条的直接承载（test_memory_admin 对拍真实 Store+Retriever）：
  - 删除一条记忆 → 下轮检索不到；
  - 编辑一条 fact → 下轮检索到**新内容**（编辑内联 retriever.forget——嵌入按
    旧内容生成，内容变了必须失效，下轮 _sync_vec 按新文本重嵌；跨进程时
    服务端检索器同样按缺行重嵌生效）；
  - 重置 → 旧记忆全不可检索（entries+hooks+dimensions 清空 + 索引重建为空）。

SQL 纪律：本模块零 SQL——全部经 MemoryStore / MemoryRetriever 既有方法
（execute 调用点内联静态字面量 + 参数绑定，纪律落点不变）。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional, Sequence

from server.memory.store import MemoryStore
from server.memory.vector import MemoryRetriever

logger = logging.getLogger(__name__)


class MemoryAdmin:
    """设置窗记忆/关系页的数据面。retriever 可选（vec 失效缝；None=纯 store）。"""

    def __init__(self, store: MemoryStore,
                 retriever: Optional[MemoryRetriever] = None) -> None:
        self.store = store
        self.retriever = retriever

    def list_entries(self, kinds: Sequence[str] = ("fact", "diary"),
                     limit: int = 500) -> list[sqlite3.Row]:
        """记忆页表格数据源（新→旧）。"""
        return self.store.entries(kinds=list(kinds), limit=limit)

    def get_entry(self, entry_id: int) -> Optional[sqlite3.Row]:
        """查看全文（store 按 id 精确取——窗口线性扫描会对「表格列出但窗口
        外」的条目静默查空，查看/编辑按钮无反应）。"""
        return self.store.get_entry_by_id(entry_id)

    def delete_entry(self, entry_id: int) -> bool:
        """删除单条（验收：下轮检索不到）。"""
        hit = self.store.delete_entry(entry_id)
        if hit:
            logger.info("记忆 #%s 已删除（设置窗）", entry_id)
        return hit

    def edit_entry(self, entry_id: int, content: str) -> bool:
        """编辑单条（GUI 仅对 fact 开放，v1.9.2 用户校对）；向量索引联动失效。"""
        hit = self.store.update_content(entry_id, str(content))
        if hit:
            try:
                self.retriever.forget(entry_id)      # 旧嵌入作废 → 下轮重嵌
            except Exception:
                logger.warning("向量索引失效失败（内容已改，检索质量暂旧）",
                               exc_info=True)
            logger.info("记忆 #%s 已编辑（设置窗，保存即生效于下轮检索）", entry_id)
        return hit

    def reset_all(self) -> dict:
        """一键重置记忆与关系（GUI 二次确认后调用）；返回各表清除行数。"""
        counts = self.store.reset_all()
        try:
            self.retriever.clear_index()
        except Exception:
            logger.warning("向量索引清空失败（旧记忆已清，索引随下轮重建）",
                           exc_info=True)
        logger.warning("记忆与关系已全部重置（%s）——旧人格锚定清除", counts)
        return counts

    def read_relationship(self) -> Optional[dict]:
        """关系页只读数据（无行=尚未初始化）。"""
        return self.store.get_relationship()

    # ---------------- 对话历史（M+3.5/查看历史窗口数据源） ----------------

    def dialogue_rounds(self, limit: int = 200) -> list[dict]:
        """最近 limit 组对话（新→旧），解析为 {id, ts, user, reply}。

        content 约定（store.record_dialogue 写入格式）：
          user 轮    → "[用户] …\n[miyako] …"
          主动/问候轮 → "[behavior] …" / "[boot] …"（用户侧记触发名）
        """
        rounds: list[dict] = []
        for row in self.store.dialogue_recent(limit=limit):
            user, reply = _parse_round(str(row["content"]))
            rounds.append({"id": int(row["id"]), "ts": str(row["ts"]),
                           "user": user, "reply": reply})
        return rounds

    def close(self) -> None:
        """释放底层连接（窗口关闭时调用；重复调用安全）。"""
        try:
            if self.retriever is not None:
                self.retriever.close()
        finally:
            self.store.close()


def _parse_round(content: str) -> tuple[str, str]:
    """content → (user, reply)：按行首标记 [用户]/[miyako]/[trigger] 拆分。"""
    user_parts: list[str] = []
    reply_parts: list[str] = []
    current = reply_parts                       # 缺省归回复侧（主动轮形态）
    for line in content.split("\n"):
        if line.startswith("[用户]"):
            current = user_parts
            user_parts.append(line[len("[用户]"):].strip())
        elif line.startswith("[miyako]"):
            current = reply_parts
            reply_parts.append(line[len("[miyako]"):].strip())
        elif line.startswith("["):
            current = reply_parts               # [behavior]/[boot] 整行属回复侧
            reply_parts.append(line)
        else:
            current.append(line)                # 多行文本的续行跟随所属侧
    return ("\n".join(user_parts), "\n".join(reply_parts))
