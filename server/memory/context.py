"""上下文预算与组装（M+1.6，Shinsekai 借鉴②④）—— Section 冻结树 + 阈值历史压缩。

compose()：按 priority 升序渲染各 Section，**超预算从 priority 最大者整段砍**
（借鉴②）；空输出自动省略；enabled=False 跳过整段；同级按插入序稳定。
预算单位 = estimate_tokens()（server/tokens.py 唯一实现，M2.6 同源）。

clamp_compact_params()：阈值历史压缩（借鉴④）的参数不变式——压缩后目标占比必须
**低于**触发阈值（否则反复压缩死循环），越界强制钳到阈值的 75% 并告警。

render_memory_notes()：检索命中 → 时间衰减标注行（「三天前你说过…」），注入
memory 槽（M+1.6 上下文预算消费，M+1.2 检索结果的自然延伸）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from server.tokens import estimate_tokens

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Section:
    """一段可渲染的 prompt 区块（冻结：构造后不可变，保持组合可预测）。

    render: 无参回调（闭包携带上下文）；空/纯空白输出自动省略；
    priority: 小者先渲染；超预算时**从 priority 最大者开始整段丢弃**。
    """

    id: str
    render: Callable[[], str]
    priority: float = 100.0
    enabled: bool = True


def compose(sections: list[Section], *,
            budget_tokens: int,
            estimator: Callable[[str], int] = estimate_tokens) -> dict:
    """预算内组装：按 priority 升序保序渲染，超预算整段丢弃（借鉴②）。

    返回 {"text": 组装文本, "kept": [id...], "dropped": [id...]}。
    贪心策略：前面的段占掉预算后，后面的整段放弃（不做段内截断——半句话
    比缺一段更破坏人格一致性）。同级 **id 必须唯一**（借鉴②双重校验之一，
    重复 id 直接 ValueError——静默去重会掩盖装配错误）。
    """
    ids = [s.id for s in sections]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"Section id 重复（同级唯一校验失败）：{dupes}")
    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    acc = 0
    for s in sorted((s for s in sections if s.enabled), key=lambda s: s.priority):
        if not callable(s.render):
            dropped.append(s.id)
            continue
        try:
            text = (s.render() or "").strip()
        except Exception:
            logger.warning("Section %s 渲染失败，整段省略", s.id, exc_info=True)
            dropped.append(s.id)
            continue
        if not text:
            continue
        cost = estimator(text) + (2 if kept else 0)   # 段间分隔开销
        if kept and acc + cost > budget_tokens:
            dropped.append(s.id)
            continue
        kept.append((s.id, text))
        acc += cost
    return {"text": "\n\n".join(t for _, t in kept),
            "kept": [i for i, _ in kept], "dropped": list(dropped)}


def user_section(text: str, *, priority: float = 100.0) -> Section:
    """用户文本节点（借鉴②注入防线入类型层）：闭包冻结原文，**永不参与
    format/模板展开**——render 返回逐字原文，不存在第二处能改写它。"""
    frozen = str(text)
    return Section(id="user", render=lambda: frozen, priority=priority)


def clamp_compact_params(threshold_ratio: float,
                         target_ratio: float) -> tuple[float, float]:
    """阈值历史压缩参数不变式（借鉴④）：threshold ∈ [0.1, 1]，target < threshold。

    压缩后必须低于触发线，否则反复压缩死循环；越界强制钳到阈值的 75% 并告警。
    """
    threshold_ratio = min(max(float(threshold_ratio), 0.1), 1.0)
    target_ratio = float(target_ratio)
    if not (0.0 < target_ratio < threshold_ratio):
        import logging as _log

        _log.getLogger(__name__).warning(
            "压缩目标占比 %.2f 不在 (0, %.2f) 内 → 钳到阈值的 75%%",
            target_ratio, threshold_ratio)
        target_ratio = threshold_ratio * 0.75
    return threshold_ratio, target_ratio


def render_memory_notes(hits: list, *,
                        now: Optional[datetime] = None) -> str:
    """检索命中 → 时间衰减标注文本（M+1.6：「三天前你说过…」注入 memory 槽）。

    hits 为 store.entries 的 Row/dict（含 ts/content）。无命中返回空串
    （调用方以空串判定整段省略）。
    """
    now = now or datetime.now()
    lines: list[str] = []
    for h in hits:
        try:
            content = str(h["content"])
            ts = h["ts"]
        except (KeyError, IndexError, TypeError):
            continue
        try:
            dt = datetime.fromisoformat(str(ts))
            days = (now - dt).days
            tag = "刚刚" if days < 1 else f"{days}天前"
        except Exception:
            tag = ""
        lines.append(f"（{tag}）{content}")
    if not lines:
        return ""
    return "你记得这些与用户有关的事：\n" + "\n".join(f"- {ln}" for ln in lines)


HISTORY_SUMMARY_MARKER = "历史对话总结"     # 压缩后历史首行的说话方标记（借鉴④）


class HistoryCompressor:
    """阈值历史压缩（借鉴④）：阈值触发 → 压到目标占比 → 保最近 N 条 →
    摘要自身限预算 → 历史留「历史对话总结」标记行。

    参数不变式经 clamp_compact_params 强制（目标 < 阈值，防反复压缩死循环）。
    summarizer 回调注入（装配方用 chat 槽一次调用）；缺失/失败 → 机械拼接
    截断兜底（压缩语义不中断）。
    """

    _ENVELOPE_PER_TURN = 4    # 每条历史的消息信封常数（借鉴③，与 tokens.py 同源策略）

    def __init__(self, *, budget_tokens: int, threshold_ratio: float = 0.8,
                 target_ratio: float = 0.6, keep_recent: int = 6,
                 summary_max_tokens: int = 160,
                 estimator: Callable[[str], int] = estimate_tokens,
                 summarizer: Optional[Callable[[list[str]], object]] = None) -> None:
        self.threshold_ratio, self.target_ratio = clamp_compact_params(
            threshold_ratio, target_ratio)
        self.budget_tokens = max(64, int(budget_tokens))
        self.keep_recent = max(2, int(keep_recent))
        self.summary_max_tokens = max(20, int(summary_max_tokens))
        self._estimator = estimator
        self._summarizer = summarizer

    def _turn_tokens(self, speaker: str, text: str) -> int:
        return self._estimator(speaker) + self._estimator(text) + self._ENVELOPE_PER_TURN

    def _tokens(self, history: list[tuple[str, str]]) -> int:
        return sum(self._turn_tokens(sp, tx) for sp, tx in history)

    def needs_compaction(self, history: list[tuple[str, str]]) -> bool:
        return self._tokens(history) > self.budget_tokens * self.threshold_ratio

    async def compress(self, history: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """超阈值 → 压缩；未超 → 原样返回（拷贝）。

        「保最近 N 条」是硬承诺（上下文连续性优先于占比目标）：摘要 + 最近 N 条
        后若仍超目标占比，保留现状并告警（不再丢最近条目）。
        """
        history = list(history)
        if not self.needs_compaction(history):
            return history
        target = max(1, int(self.budget_tokens * self.target_ratio))
        keep = history[-self.keep_recent:]
        old = history[:-self.keep_recent]
        out: list[tuple[str, str]] = []
        if old:
            summary = await self._make_summary(old)
            out.append((HISTORY_SUMMARY_MARKER, summary))
        out.extend(keep)
        if self._tokens(out) > target:
            logger.warning("历史压缩后仍超目标占比（%d > %d）——保最近 %d 条为硬承诺，不再裁剪",
                           self._tokens(out), target, self.keep_recent)
        return out

    async def _make_summary(self, old: list[tuple[str, str]]) -> str:
        lines = [f"{sp}：{tx}" for sp, tx in old]
        if self._summarizer is not None:
            try:
                summary = await self._summarizer(lines)
                if summary and str(summary).strip():
                    return self._cap(str(summary).strip())
            except Exception:
                logger.warning("历史摘要生成失败，机械截断兜底", exc_info=True)
        return self._cap("；".join(lines))

    def _cap(self, text: str) -> str:
        """摘要自身限预算：截到 summary_max_tokens 内（按估算器二分反推前缀）。"""
        if self._estimator(text) <= self.summary_max_tokens:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._estimator(text[:mid]) <= self.summary_max_tokens:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + "…"
