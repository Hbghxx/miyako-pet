"""token 统一估算器（§0.4 决策：**全项目唯一实现**，禁止第二份）。

消费方：llm_api 计量（厂商不报 usage 时兜底，M2.2）、M+1.6 上下文预算组装
（Section 预算按本估算器计）。公式（plan §0.4 定版）：CJK 字符 1 token/字、
其余字符 len//4。

信封化（v1.9.3 借鉴③）：``estimate_messages_tokens()`` 按 chat.completions
消息列表估算——每条消息 +4 信封常数、整体 +2 基数、role 计入；多模态内容
先清洗为占位符再计数（防图片 base64 撑爆估算）。保守回落：无法结构化解析
的内容按原文本估算。
"""
from __future__ import annotations

import json

# CJK 判定区间（预编译成单个字符集语义；用区间判断避免 6 万项集合的构建成本）
_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK 符号与标点（「」、。、……）
    (0x3040, 0x309F),   # 平假名
    (0x30A0, 0x30FF),   # 片假名
    (0x3400, 0x4DBF),   # 汉字扩展 A
    (0x4E00, 0x9FFF),   # CJK 统一汉字
    (0xAC00, 0xD7AF),   # 谚文音节
    (0xF900, 0xFAFF),   # CJK 兼容表意文字
    (0xFF00, 0xFFEF),   # 全角形式（！？ＡＢ１２）
)

# chat.completions 信封常数（v1.9.3 借鉴③：与厂商官方 cookbook 口径一致）
_MESSAGE_ENVELOPE_TOKENS = 4
_CHAT_BASE_TOKENS = 2

# 多模态内容中按占位符计数的键（图片 base64 等大体积载荷）
_MEDIA_KEYS = ("image_url", "input_image", "data", "media")
_MEDIA_PLACEHOLDER = "[media omitted]"


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= code <= hi:
            return True
    return False


def estimate_tokens(text: str) -> int:
    """CJK 1 token/字 + 其余字符 len//4（向下取整，§0.4 字面公式）。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk_char(ch):
            cjk += 1
        else:
            other += 1
    return cjk + other // 4


def _scrub_media(value):
    """递归清洗多模态载荷：图片 base64 等大字段换成占位符（计数不被撑爆）。"""
    if isinstance(value, dict):
        return {k: (_MEDIA_PLACEHOLDER if k in _MEDIA_KEYS else _scrub_media(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_media(v) for v in value]
    return value


def estimate_messages_tokens(messages: list) -> int:
    """估算一条 chat.completions 请求的消息列表 token（信封化：每条 +4、整体 +2）。

    content 为字符串直接估算；多模态（list/dict）先清洗媒体占位符再估算。
    """
    total = _CHAT_BASE_TOKENS
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        total += _MESSAGE_ENVELOPE_TOKENS
        total += estimate_tokens(str(m.get("role") or ""))
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif content is not None:
            total += estimate_tokens(json.dumps(_scrub_media(content),
                                                ensure_ascii=False))
    return total
