"""记忆校对去重（M+1.4，借鉴⑤后台修复重试）—— 独立模块。

每日后台：收集近 N 天 fact（复用 store.facts_recent 唯一查询方法）→
喂 correction 槽 LLM 比对 → 输出 JSON 计划 {merge, delete} → 按计划落库。

协议（LLM 输出）：{"merge": [{"keep_id": x, "drop_ids": [y...], "content": 合并后文本}],
"delete": [id...]}。防线：id 必须来自清单（越界忽略）、keep≠drop、修复重试上限 2 次
（借鉴⑤：原输出 append 回去 + 升级措辞 + 全新消息列表不污染持久历史）、
失败/非法 → 静默跳过本次（返回 None），store 零改动。SQL 全参数绑定。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_CORRECTION_INSTRUCTION = (
    "以下是长期记忆库近 3 天的条目（每行格式「id: 内容」）。请找出语义重复、"
    "或互相矛盾的条目对，输出一个 JSON 对象，且只输出 JSON：\n"
    '{"merge": [{"keep_id": 保留条目的id, "drop_ids": [被合并掉的id, ...], '
    '"content": "合并后的条目文本"}], "delete": [应整条删除的id, ...]}\n'
    "规则：所有 id 必须来自上面的清单；没有需要处理的内容就输出 "
    '{"merge": [], "delete": []}；不要输出 JSON 以外的任何文字。'
)
_CORRECTION_REPAIR = (
    "你上一条输出不是合法的处理计划。重新输出：只含一个 JSON 对象，"
    '格式 {"merge": [{"keep_id": x, "drop_ids": [y], "content": "文本"}], "delete": [id]}，'
    "所有 id 必须来自上面的清单；没有可处理的就输出空数组。不要输出任何其他文字。"
)


def _strip_json_fences(text: str) -> str:
    """剥掉 LLM 常见的 ```json 围栏（宽容解析）。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def parse_correction_plan(output: str, valid_ids: set[int]) -> Optional[dict]:
    """解析并消毒 LLM 的校对计划；非法 → None（越界 id 忽略、结构非法整单拒绝）。"""
    raw = _strip_json_fences(output)
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    merge_raw = obj.get("merge")
    delete_raw = obj.get("delete")
    if not isinstance(merge_raw, list) or not isinstance(delete_raw, list):
        return None
    merge: list[dict] = []
    keep_ids: set[int] = set()
    drop_ids: set[int] = set()
    for item in merge_raw:
        if not isinstance(item, dict):
            continue
        try:
            keep = int(item.get("keep_id"))
        except (TypeError, ValueError):
            continue
        drops_raw = item.get("drop_ids") or []
        drops: list[int] = []
        for d in drops_raw:
            try:
                drops.append(int(d))
            except (TypeError, ValueError):
                continue
        content = str(item.get("content") or "").strip()
        if keep not in valid_ids or keep in drop_ids or not drops or not content:
            continue
        drops = [d for d in drops if d in valid_ids and d != keep]
        if not drops:
            continue
        keep_ids.add(keep)
        drop_ids.update(drops)
        merge.append({"keep_id": keep, "drop_ids": drops, "content": content})
    delete: list[int] = []
    for d in delete_raw:
        try:
            did = int(d)
        except (TypeError, ValueError):
            continue
        if did in valid_ids and did not in keep_ids and did not in drop_ids:
            delete.append(did)
    return {"merge": merge, "delete": delete}


class Deduplicator:
    """每日后台矛盾清洗。``run`` 由每日触发器调用（M+1.5 每日校对委托同此入口）。"""

    def __init__(self, store, llm, *,
                 notify: Optional[Callable[[str], None]] = None,
                 sleep: Callable[[float], object] = None,
                 on_content_changed: Optional[Callable[[int], None]] = None) -> None:
        import asyncio

        self.store = store
        self.llm = llm
        self._sleep = sleep or asyncio.sleep
        self.notify = notify or (lambda msg: logger.info("%s", msg))
        # 内容改写回调（merge 成功后对 keep_id 调用）：接线 MemoryRetriever.forget
        # 使向量索引同步失效——漏接则检索永远按合并前的旧嵌入给该条打分
        self._on_content_changed = on_content_changed

    async def run(self, *, days: float = 3.0, max_facts: int = 40) -> Optional[dict]:
        """跑一轮校对；返回 {"merged": n, "deleted": n}，无需校对/失败返回 None。

        静默语义：LLM 异常/计划非法 → 本轮跳过（store 零改动），不抛异常。
        """
        facts = self.store.facts_recent(days)[:max_facts]
        if len(facts) < 2:
            return None                       # 少于 2 条无从比对
        valid_ids = {row["id"] for row in facts}
        listing = "\n".join(f"{row['id']}: {row['content']}" for row in facts)
        prompt = _CORRECTION_INSTRUCTION + "\n\n" + listing
        history: Optional[list[tuple[str, str]]] = None

        plan: Optional[dict] = None
        try:
            for attempt in range(2):              # 借鉴⑤：上限 2 次（首试 + 1 次修复）
                output = "".join([chunk async for chunk in
                                  self.llm.stream_lines(prompt, history=history)])
                plan = parse_correction_plan(output, valid_ids)
                if plan is not None:
                    break
                if attempt == 0:
                    prompt = _CORRECTION_REPAIR + "\n\n" + listing
                    history = [("assistant", output)]
                await self._sleep(0)              # 轮间查取消
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("记忆校对 LLM 调用失败（静默跳过本轮）", exc_info=True)
            return None
        if plan is None:
            self.notify("记忆校对：连续两次输出非法计划，本轮跳过")
            return None

        merged = deleted = 0
        touched: set[int] = set()
        for item in plan["merge"]:
            if self.store.update_content(item["keep_id"], item["content"]):
                merged += 1
                if self._on_content_changed is not None:
                    try:
                        self._on_content_changed(item["keep_id"])
                    except Exception:
                        logger.debug("内容改写回调失败（不影响合并）", exc_info=True)
            for d in item["drop_ids"]:
                if d in touched:
                    continue
                if self.store.delete_entry(d):
                    deleted += 1
                touched.add(d)
        for d in plan["delete"]:
            if d not in touched and self.store.delete_entry(d):
                deleted += 1
            touched.add(d)
        self.notify(f"记忆校对完成：合并 {merged} 处、删除 {deleted} 条")
        return {"merged": merged, "deleted": deleted}
