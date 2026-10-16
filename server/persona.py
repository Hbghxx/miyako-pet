"""persona（M2.1）—— character_card → system prompt 的唯一组装点。

原则 11：``.char`` 包 character_card.yaml 的**读取入口收敛在本模块**——
orchestrator（tone_examples 兜底消费方）等一律经此读卡，禁止第二处直读。
M-CHA.1 已落地：默认卡路径经 server.constants.resolve_character_package() 解析。

组装顺序 = prompt 段序：
  角色设定(name/description/persona) → 性格 → 价值观 → 禁忌 → 说话方式
  → 语气示例(tone_examples，dialog_v1 行式 few-shot) → 格式约束段（必居尾）。
format_prompt_reminder 居尾是 M2.1 验收点：LLM 对 prompt 尾部的格式指令遵循度最高。

fail-open（原则 3 外推，用户已批准）：卡缺失/损坏/无可用段落 → 角色中性最小
prompt + warning（启动构建一次，天然只警一次，无需防刷屏）。最小兜底**禁止
硬编码任何角色名**——M-CHA 角色插拔后本模块按包参数化，写死角色名即 v1.9 违规。
corpus_tone_examples（语料溯源段）不进 prompt——它是 M+1 原声台词兜底的数据源
（character_card.yaml 段落分工注释）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from server.constants import resolve_character_package
from server.contracts.base import build_contract

logger = logging.getLogger(__name__)

# 激活角色包经 resolver（M-CHA.1）；卡读取入口仍收敛本模块（原则 11）
DEFAULT_CARD_PATH = resolve_character_package() / "character_card.yaml"

# 角色中性最小兜底（卡缺失/损坏时）——不许出现任何具体角色名（v1.9 约束）
MINIMAL_FALLBACK_PROMPT = "你是一个运行在用户桌面上的陪伴角色，请用中文与用户自然对话。"

# UI 展示名的键级优先序（M-CHA.2）：与 _collect_sections 头部行同序
_DISPLAY_NAME_KEYS = ("display_name", "name_zh", "name")

# character_card 顶层字段的运行时消费清单（v1.9.6 固化）——跨仓产消契约的 pet 半边：
# miyako_life 产线的 interface.yaml 消费断言与本清单配对，变更时先改此处再动代码。
# 注意：honesty_boundary **有意不渲染**（2026-09-12 裁定：不进 prompt），
# corpus_tone_examples 为原声台词兜底预留（M+ 观望）——二者均非遗漏。
CONSUMED_CARD_FIELDS = (
    "display_name", "name_zh", "name",      # 角色名（优先序即回退序）
    "description", "persona",               # 【角色设定】
    "personality", "behavior_rules",        # 【性格】【行为准则】
    "values", "taboos", "anti_patterns",    # 【价值观】【禁忌】【角色反模式】
    "speech_style",                         # 【说话方式】（first_person/calls_you/register/politeness_ratio/notes）
    "tone_examples",                        # 【语气示例】
)


def display_name_from_package(pkg_dir: Path) -> str:
    """读指定角色包的展示名（M-CHA.4：切换子菜单枚举用）；fail-open 包目录名兜底。"""
    card = Persona._load_card(pkg_dir / "character_card.yaml")
    if card:
        for key in _DISPLAY_NAME_KEYS:
            value = str(card.get(key) or "").strip()
            if value:
                return value
    return pkg_dir.name.removesuffix(".char")


def display_name() -> str:
    """激活角色展示名——UI 文案（托盘/气泡/弹窗/诊断）的唯一数据源（M-CHA.2）。

    card.display_name → name_zh → name → **resolver 解析的包 id** 兜底
    （fail-open；回退走 resolver 而非卡片文件目录名——卡路径可被测试注入到
    任意目录，目录名不是包 id）。产品级标识（互斥量/日志目录/bat/产品名）
    **不**消费本函数——产品名与角色名解耦是 plan M-CHA.2 定案。
    """
    card = Persona._load_card(DEFAULT_CARD_PATH)
    if card:
        for key in _DISPLAY_NAME_KEYS:
            value = str(card.get(key) or "").strip()
            if value:
                return value
    return resolve_character_package().name.removesuffix(".char")


class Persona:
    """character_card 消费门面：``system_prompt``（构建一次）+ ``tone_examples``。"""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.card_path = config_path or DEFAULT_CARD_PATH
        self.card = self._load_card(self.card_path)
        self.tone_examples: list[str] = self._extract_tone_examples(self.card)
        self.system_prompt = self._build_system_prompt()

    # ---------------- 读卡（fail-open） ----------------
    @staticmethod
    def _load_card(path: Path) -> Optional[dict]:
        try:
            if not path.is_file():
                logger.warning("character_card 不存在：%s → 角色中性最小 prompt", path)
                return None
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                logger.warning("character_card 为空或非映射：%s → 角色中性最小 prompt", path)
                return None
            return data
        except Exception:
            logger.warning("character_card 解析失败：%s → 角色中性最小 prompt",
                           path, exc_info=True)
            return None

    @staticmethod
    def _extract_tone_examples(card: Optional[dict]) -> list[str]:
        """9 情绪 × N 条 dialog_v1 行式拍平；段缺失/非法 → 空表（orchestrator 有内置兜底）。"""
        section = (card or {}).get("tone_examples") or {}
        try:
            return [str(line) for lines in section.values() for line in (lines or [])]
        except Exception:
            logger.warning("tone_examples 段非法，忽略", exc_info=True)
            return []

    # ---------------- prompt 组装 ----------------
    def _build_system_prompt(self) -> str:
        sections = self._collect_sections()
        if not sections:
            logger.warning("character_card 无可用段落 → 角色中性最小 prompt")
            body = MINIMAL_FALLBACK_PROMPT
        else:
            body = "\n\n".join(sections)
        reminder = build_contract().format_prompt_reminder()
        return f"{body}\n\n{reminder}"  # reminder 必居尾（M2.1 验收点）

    def _collect_sections(self) -> list[str]:
        card = self.card
        if not card:
            return []
        sections: list[str] = []

        def bullet(title: str, items) -> None:
            rows = [str(x).strip() for x in (items or []) if str(x).strip()]
            if rows:
                sections.append(f"【{title}】\n" + "\n".join(f"- {row}" for row in rows))

        header_rows: list[str] = []
        name = card.get("display_name") or card.get("name_zh") or card.get("name")
        if name:
            header_rows.append(f"你扮演的角色：{name}")
        if str(card.get("description") or "").strip():
            header_rows.append(str(card["description"]).strip())
        if str(card.get("persona") or "").strip():
            header_rows.append(str(card["persona"]).strip())
        if header_rows:
            sections.append("【角色设定】\n" + "\n".join(header_rows))

        bullet("性格", card.get("personality"))

        # 行为准则（life 卡 P4 层，v1.9.6 接入消费）：if-then 情境反应规则
        rule_rows: list[str] = []
        for rule in card.get("behavior_rules") or []:
            if isinstance(rule, dict):
                cond = str(rule.get("if") or "").strip()
                act = str(rule.get("then") or "").strip()
                if cond and act:
                    rule_rows.append(f"若{cond}，{act}。")
            elif str(rule).strip():
                rule_rows.append(str(rule).strip())
        if rule_rows:
            sections.append("【行为准则】\n" + "\n".join(f"- {row}" for row in rule_rows))

        bullet("价值观", card.get("values"))
        bullet("禁忌（必须遵守）", card.get("taboos"))
        bullet("角色反模式（出现即违规）", card.get("anti_patterns"))

        style = card.get("speech_style")
        if isinstance(style, dict):
            rows: list[str] = []
            if style.get("first_person"):
                rows.append(f"自称：{style['first_person']}")
            if style.get("calls_you"):
                # 数据直进 prompt（语料实测定调）：用户改称呼只需改卡片，代码不做任何替换
                rows.append(f"称呼用户为：{style['calls_you']}")
            if style.get("register"):
                rows.append(f"语体：{style['register']}")
            if style.get("politeness_ratio") is not None:
                rows.append(f"丁寧語率参考值：{style['politeness_ratio']}")
            if str(style.get("notes") or "").strip():
                rows.append(str(style["notes"]).strip())
            if rows:
                sections.append("【说话方式】\n" + "\n".join(f"- {row}" for row in rows))

        if self.tone_examples:
            sections.append("【语气示例（每行一条，模仿语气与输出格式）】\n"
                            + "\n".join(self.tone_examples))
        return sections
