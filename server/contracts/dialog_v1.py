"""dialog_v1 契约 —— 行式输出 ``[e:情绪]中文|日本語`` 的唯一实现（M0.3）。

脏行修复规则（全部 fail-open，不抛异常）：
  - 缺 ``|``        → 含假名=模型漏配的纯日文行 → 只朗读不上屏（text_zh=""）；
                      不含假名 → 整行当中文，tts_ok=False；
  - 未知表情        → normal（对照 server.emotion_defs 闭集）；
  - 缺 ``[e:]``     → 补 normal，整行仍按「中文|日本語」尝试拆分；
  - **多段（3 个及以上 ``|`` 分隔，v1.9.4）** → 按段内**是否含假名**归类
    （含假名 → text_ja 桶、不含 → text_zh 桶，桶内按原顺序以 ``\n`` 连接，
    钉死于 test_multi_pipes_classified_by_kana）——与位置无关，``ja|zh``
    顺序的行也能正确归边；旧规则"以第一个 | 为界，右侧全归 text_ja"
    会把第 2+ 句中文并进 TTS 文本，表现为「语音比气泡多出几句话」。
    （已知启发式局限：纯汉字日文片段会落到中文桶 → 气泡侧，可接受。）
  - 空行            → 空 ParsedLine（tts_ok=False），由 orchestrator 决定跳过；
  - 其它任何解析异常 → 整行当中文，tts_ok=False，仅 warning。

竖线常规形态以**第一个** ``|`` 为界：右侧（含后续竖线）全部归 text_ja，
避免日文台词里出现竖线时被错误截断——两段形态行为与历史完全一致。
"""
from __future__ import annotations

import logging
import re

from server.contracts.base import OutputContract, ParsedLine
from server.emotion_defs import DEFAULT_EMOTION, SENTIMENT_CLOSE_SET

logger = logging.getLogger(__name__)

_EMOTION_TAG_RE = re.compile(r"^\s*\[e:([^\]]*)\]")


def _has_kana(text: str) -> bool:
    """段内是否含假名（ひらがな U+3040-309F / カタカナ U+30A0-30FF）——
    日文台词几乎必含假名、中文必不含，是多段行归侧的可靠判别器。"""
    return any(0x3040 <= ord(ch) <= 0x30FF for ch in text)


class DialogV1Contract(OutputContract):
    name = "dialog_v1"

    def format_prompt_reminder(self) -> str:
        emotions = "/".join(SENTIMENT_CLOSE_SET)
        return (
            "【输出格式（严格遵守）】\n"
            "每一行输出一句台词，格式：[e:情绪]中文|日本語\n"
            f"情绪只能取：{emotions}\n"
            "中文与日本語用半角竖线 | 分隔；日文台词内不要再出现竖线。\n"
            "日本語必须是中文那句话的对应翻译：内容一致，日文侧不添加中文侧"
            "没有的额外内容，也不遗漏（气泡只显示中文，朗读只用日本語）。\n"
            "禁止输出解释、动作描写、代码块或空行。"
            "缺失情绪标签或使用未知情绪都会按 normal 处理；缺失竖线该行将无法朗读。"
        )

    def parse(self, raw_line: str) -> ParsedLine:
        try:
            line = (raw_line or "").strip()
            if not line:
                return ParsedLine(emotion=DEFAULT_EMOTION, text_zh="", text_ja="", tts_ok=False)

            match = _EMOTION_TAG_RE.match(line)
            if match:
                emotion = match.group(1).strip()
                if emotion not in SENTIMENT_CLOSE_SET:
                    logger.warning("未知表情 %r → normal（闭集见 emotion_defs）", emotion)
                    emotion = DEFAULT_EMOTION
                content = line[match.end():].strip()
            else:
                emotion = DEFAULT_EMOTION  # 缺 [e:] → 补 normal
                content = line

            if "|" in content:
                segments = [seg.strip() for seg in content.split("|") if seg.strip()]
                if len(segments) == 2:
                    zh, ja = segments                     # 常规：zh|ja 一一分隔
                else:
                    # 🔑 多段（3 段及以上，v1.9.4 真机定位）：旧规则"以第一个 |
                    # 为界"会把第 2+ 句中文并进 TTS 文本——「语音比气泡多几句」。
                    # 按假名判别归类：含假名（ひらがな/カタカナ）→ 日文侧（合成），
                    # 否则 → 中文侧（气泡/显示），各按原顺序 \n 连接，文本零丢失。
                    zh_parts: list[str] = []
                    ja_parts: list[str] = []
                    for seg in segments:
                        (ja_parts if _has_kana(seg) else zh_parts).append(seg)
                    zh = "\n".join(zh_parts)
                    ja = "\n".join(ja_parts)
                    if ja_parts and zh_parts:
                        logger.warning("多段行按假名判别归侧（zh %d 段 / ja %d 段）",
                                       len(zh_parts), len(ja_parts))
                zh, ja = zh.strip(), ja.strip()
                tts_ok = bool(ja)  # 竖线在但日文侧为空：无内容可朗读
                if not ja:
                    logger.warning("竖线后日文为空，tts_ok=False: %.40r", content)
            else:
                # 缺 | → 若整行含假名：模型漏配的纯日文行 → 只朗读不上屏
                #（真机 2026-09-12：日文原句直接显示进气泡）；否则整行当中文
                if _has_kana(content):
                    zh, ja, tts_ok = "", content, True
                    logger.warning("无竖线的日文行 → 只朗读不上屏: %.40r", content)
                else:
                    zh, ja, tts_ok = content, "", False
                    logger.warning("缺分隔符 | → 整行当中文，tts_ok=False: %.40r",
                                   content)

            return ParsedLine(emotion=emotion, text_zh=zh, text_ja=ja, tts_ok=tts_ok)
        except Exception:
            # 失败不抛异常（§4.1）：整行当中文走气泡，行缓冲续走（orchestrator）
            logger.exception("dialog_v1 单行解析异常，按脏行降级")
            fallback = (raw_line or "").strip()
            return ParsedLine(emotion=DEFAULT_EMOTION, text_zh=fallback, text_ja="", tts_ok=False)
