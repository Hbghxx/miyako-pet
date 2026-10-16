"""voice_postprocess（M1.8）—— 台词的机械修正层（TTS 合成源与气泡显示源共用）。

管线顺序是**编译期约束**（plan M1.8 定案，顺序固定，禁止配置动态排序）：
    strip_actions → glossary → quote_guard 掩蔽 → replace_map → verbal_tics
想调顺序必须改代码（重排会钉死顺序的测试变红）。

要点：
  - 双语分节：glossary / replace_map 按 zh/ja 各自语言节生效；
  - 专有名词豁免**跨语言一致**：掩蔽模式取所有语言节 glossary key 的并集——
    zh 节命中的 "AI" 在 ja 侧同样被豁免，否则双语口癖会裂开；
  - quote_guard 掩蔽引语区（「」/“”/"..."）+ 专有名词命中区间，
    replace_map 跳过掩蔽区（占位符方案）；
  - verbal_tics：概率后缀（zh "……哼" / ja "……ふん"，默认 0.3，
    RNG 可注入固定种子——演出化妆性随机，与 M1.3 同裁定）；
  - fail-open：配置缺失/非法/运行异常 → 原文直通不阻断；
    托盘 ⚠️ 仅第一次（provider 级单例 flag，防 orchestrator 高频刷屏）。
"""
from __future__ import annotations

import logging
import random
import re
from pathlib import Path
from typing import Callable, Optional

import yaml

from server.constants import resolve_character_package

logger = logging.getLogger(__name__)

TTS_CONFIG_PATH = resolve_character_package() / "tts_config.yaml"

# 掩蔽区引语模式（plan：引语区「」/""）；未闭合的「防御性掩蔽到串尾
_QUOTE_RE = re.compile(r"「[^」]*」|“[^”]*”|\"[^\"]*\"|「[^」]*$")
_MASK_MARK = "\x00"


class VoicePostprocess:
    """机械修正管线。构建一次、每句调用 ``process(text, lang)``。"""

    _warned_broken = False  # provider 级单例 flag：失败提示只发一次（防刷屏）

    def __init__(self, config_path: Path | None = None,
                 notify: Optional[Callable[[str], None]] = None,
                 rng: Optional[random.Random] = None) -> None:
        self._notify = notify or (lambda msg: logger.warning("%s", msg))
        self._rng = rng or random.Random()
        self._config_path = config_path or TTS_CONFIG_PATH
        self._tic_used = False   # M2.6④：reply 级口癖节流（每 reply 至多一次）
        rules, reason = self._load_rules(self._config_path)
        self._broken = rules is None
        if self._broken:
            self._replace = {"zh": {}, "ja": {}}
            self._glossary = {"zh": {}, "ja": {}}
            self._tic_suffix = {"zh": "", "ja": ""}
            self._tic_probability = 0.0
            self._protected_re = None
            self._notify_broken_once(reason or "配置不可用")
            return
        try:
            self._apply_rules(rules)
        except Exception as exc:  # 结构/类型校验失败同走 fail-open 单例提示
            self._replace = {"zh": {}, "ja": {}}
            self._glossary = {"zh": {}, "ja": {}}
            self._tic_suffix = {"zh": "", "ja": ""}
            self._tic_probability = 0.0
            self._protected_re = None
            self._notify_broken_once(f"配置结构非法：{exc}")

    # ---------------- 配置加载与校验 ----------------
    @classmethod
    def reset_warn_flag(cls) -> None:
        """测试辅助：复位单例提示 flag。"""
        cls._warned_broken = False

    def _load_rules(self, path: Path) -> tuple[Optional[dict], Optional[str]]:
        try:
            if not path.is_file():
                return None, f"配置文件不存在：{path}"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return None, f"配置解析失败：{exc.__class__.__name__}"
        if not isinstance(data, dict):
            return None, "配置不是映射结构"
        return data, None

    def _apply_rules(self, cfg: dict) -> None:
        def lang_dicts(key: str) -> dict[str, dict[str, str]]:
            section = cfg.get(key) or {}
            if not isinstance(section, dict):
                raise ValueError(f"{key} 段必须是映射")
            out: dict[str, dict[str, str]] = {}
            for lang in ("zh", "ja"):
                table = section.get(lang) or {}
                if not isinstance(table, dict):
                    raise ValueError(f"{key}.{lang} 必须是映射")
                out[lang] = {str(k): str(v) for k, v in table.items()}
            return out

        self._replace = lang_dicts("replace_map")
        self._glossary = lang_dicts("glossary")

        tics = cfg.get("verbal_tics") or {}
        if not isinstance(tics, dict):
            raise ValueError("verbal_tics 段必须是映射")
        self._tic_probability = float(tics.get("probability", 0.3))
        self._tic_suffix = {"zh": str(tics.get("zh", "")),
                            "ja": str(tics.get("ja", ""))}

        # 专有名词豁免：glossary 的 KEY（跨语言一致命中）∪ VALUE（替换结果）
        # ——key 保护"ja 侧也认 zh 节登记的词"，value 保护"glossary 替换结果不被
        # replace_map 改坏"。长词优先。
        terms: set[str] = set()
        for table in self._glossary.values():
            terms.update(table.keys())
            terms.update(table.values())
        ordered = sorted(terms, key=len, reverse=True)
        self._protected_re = (re.compile("|".join(re.escape(t) for t in ordered))
                              if ordered else None)

    def _notify_broken_once(self, reason: str) -> None:
        self._broken = True
        if not VoicePostprocess._warned_broken:
            VoicePostprocess._warned_broken = True
            self._notify(f"voice_postprocess 配置不可用（{reason}），"
                         "已原文直通不阻断；本提示仅出现一次")

    # ---------------- 固定管线（顺序勿动，见模块 docstring） ----------------
    def begin_reply(self) -> None:
        """M2.6④ reply 级口癖节流：每个 reply 至多附加一次，固定规则不加配置。

        orchestrator 在 reply 开始时调用；跨 reply 不复位会导致只有第一句
        有机会带口癖——调用方语义即「一次对话」。
        """
        self._tic_used = False

    def process(self, text: str, lang: str = "zh") -> str:
        if self._broken:
            return text  # 原文直通（fail-open）
        try:
            step1 = self._strip_actions(text)
            step2 = self._apply_glossary(step1, lang)
            masked, spans = self._quote_guard(step2)
            step4 = self._apply_replace_map(masked, lang)
            step5 = self._apply_verbal_tics(step4, lang)
            return self._unmask(step5, spans)
        except Exception:
            logger.exception("voice_postprocess 运行异常，原文直通")
            self._notify_broken_once("运行期异常")
            return text

    def _strip_actions(self, text: str) -> str:
        """剥动作标记：（...）/ (...) / *...*。"""
        return re.sub(r"（[^）]*）|\([^)]*\)|\*[^*]+\*", "", text or "").strip()

    def _apply_glossary(self, text: str, lang: str) -> str:
        # 长词优先，避免短键吃掉长键的前缀
        for key in sorted(self._glossary.get(lang, {}), key=len, reverse=True):
            text = text.replace(key, self._glossary[lang][key])
        return text

    def _quote_guard(self, text: str) -> tuple[str, list[str]]:
        """掩蔽引语区 + 专有名词命中区间 → 占位符；返回 (占位文本, 原文片段表)。"""
        spans: list[tuple[int, int]] = [
            (m.start(), m.end()) for m in _QUOTE_RE.finditer(text)]
        if self._protected_re is not None:
            spans += [(m.start(), m.end())
                      for m in self._protected_re.finditer(text)]
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if end <= start:
                continue
            if merged and start < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        parts: list[str] = []
        originals: list[str] = []
        prev = 0
        for i, (start, end) in enumerate(merged):
            parts.append(text[prev:start])
            parts.append(f"{_MASK_MARK}{i}{_MASK_MARK}")
            originals.append(text[start:end])
            prev = end
        parts.append(text[prev:])
        return "".join(parts), originals

    def _apply_replace_map(self, text: str, lang: str) -> str:
        # 掩蔽区此时是占位符，str.replace 天然跳过它们
        for key in sorted(self._replace.get(lang, {}), key=len, reverse=True):
            text = text.replace(key, self._replace[lang][key])
        return text

    def _apply_verbal_tics(self, text: str, lang: str) -> str:
        suffix = self._tic_suffix.get(lang, "")
        if not suffix or not text.strip():
            return text
        if self._tic_used:                # M2.6④：本 reply 已附加过 → 直接跳过
            return text
        if self._rng.random() < self._tic_probability:
            self._tic_used = True
            return text + suffix
        return text

    @staticmethod
    def _unmask(text: str, originals: list[str]) -> str:
        for i, original in enumerate(originals):
            text = text.replace(f"{_MASK_MARK}{i}{_MASK_MARK}", original)
        return text
