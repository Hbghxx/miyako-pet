"""SillyTavern 酒馆卡适配（2026-09-18 用户需求）：ST 卡 → miyako 角色包。

支持两种实卡形态：
  - ``.json``：chara_card_v1（顶层字段）/ v2 / v3（``data`` 字段承载，顶层为
    兼容镜像）——归一化一律优先 ``data``
  - ``.png``：卡数据内嵌 PNG 文本块（v1/v2 关键字 ``chara``，v3 关键字
    ``ccv3``，值 = base64(JSON)）——tEXt / iTXt / zTXt 三种块都认，
    同关键字多块拼接后解码；``ccv3`` 优先于 ``chara``

转换产物 = 完整 .char 包（character_card / manifest / relationship /
tts_config / sprites 五件套），随后走 card_importer 的 scan + import 既有链
（回滚保护、激活校验、重启编排全部继承）。

字段映射（pet 侧消费契约 persona.py CONSUMED_CARD_FIELDS 内，零新增字段）：
  ST name                     → display_name / name
  ST description              → persona 主体（宏替换后原样保留）
  ST scenario / system_prompt → persona 追加小节（【场景】/【系统提示】）
  ST first_mes                → persona 追加小节（【开场白】）
  ST mes_example              → persona 追加小节（【对话风格参考】——原样保留
                                是语气保真的最强信号；输出格式仍由契约尾部
                                reminder 约束，小节标题里再钉一次）
  ST personality              → personality（逐行拆条）
  ST post_history_instructions→ behavior_rules（逐条拆行，剥序号）
  tags / creator / version    → manifest.yaml 溯源元数据
  character_book（世界书）    → 不导入（v1 范围外，manifest 里如实注明）

宏替换：``{{char}}``/``{char}`` → 角色名，``{{user}}``/``{user}`` → 「用户」
（miyako 无 ST 宏引擎，残花括号会让模型困惑）。

音色策略（2026-09-18 用户定案：**酒馆角色可以借用 miyako 的音色**）：
  - 生成的 tts_config.yaml **不带** gptsovits 段——运行时由 GptsovitsTts 的
    候选池兜底（v1.9.8 既有机制）接管：导入对话框默认从当前激活包拷
    reference_audio 候选池（import_card 的 reference_donor 现成机制），
    GPT-SoVITS 服务里加载的正是 miyako 权重、参考音频同源 → 借用即成立
  - 不借用（用户取消勾选/无供体）→ 包内无参考配置 → 服务在时逐 reply
    降级纯气泡（既有降级矩阵行为）
  - PNG 卡的卡面图 → sprites/card.<原扩展名>，九情绪统一映射（单图立绘）；
    JSON 卡自动探测同名 .png 兄弟；**art_path 可显式指定任意图片覆盖**
    （用户自选立绘，2026-09-18）；三者皆无 → 拷 data/fallback 占位图九张
"""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import struct
import zlib
from pathlib import Path

from client.card_importer import sanitize_package_id
from server.constants import APP_ROOT

logger = logging.getLogger(__name__)

REPO_ROOT = APP_ROOT
FALLBACK_SPRITES_DIR = REPO_ROOT / "data" / "fallback" / "sprites"
DEFAULT_TTS_DIR = REPO_ROOT / "data" / "tts"   # 默认音色权重（酒馆卡无配套 TTS 时接线用）

# 九元情绪闭集（server/emotion_defs.py 唯一定义；此处仅为生成 sprites 映射，
# 不做校验——校验权在 emotion_defs，客户端生成器按同值枚举）
_EMOTIONS = ("normal", "happy", "shy", "pout", "angry", "sad",
             "surprise", "thinking", "sleepy")

# 产品级默认护栏（导入卡自身规则之外的底线；不注入人设向约束——
# 角色是否浪漫陪伴由卡自己说了算）
_DEFAULT_TABOOS = (
    "不输出违反法律或危害现实世界的指导",
    "动作、神态与心理描写放进括号或星号内，正文只写台词（语音层会剥离非台词部分）",
)

_DEFAULT_BRANCHES = (
    ("初识", 0, 20, "礼貌但保持距离，回应收敛，不多问私事。"),
    ("熟悉", 20, 50, "放松自然，会聊日常琐事，偶尔小小吐槽一句。"),
    ("亲近", 50, 80, "把你当重要的人，主动分享与关心。"),
    ("羁绊", 80, 100, "完全信任，会撒娇也会认真吃醋。"),
)


class TavernCardError(ValueError):
    """酒馆卡解析/转换失败（用户可见原因，对话框直接展示）。"""


def find_default_tts_weights(root: Path | str | None = None
                             ) -> tuple[Path | None, Path | None]:
    """默认 TTS 权重发现（2026-09-18 用户需求）：扫 data/tts/（或注入 root）。

    返回 (sovits .pth, gpt .ckpt)，各取字典序第一个；任一缺失为 None。
    酒馆卡导入的卡无配套权重时，对话框把 GPT-SoVITS 服务接线到这对权重
    （wire_tts_service 复用，零新机制）→ 出声即默认音色。
    """
    base = Path(root) if root is not None else DEFAULT_TTS_DIR
    sovits_dir = base / "SoVITS_weights_v2"
    gpt_dir = base / "GPT_weights_v2"
    sovits = sorted(sovits_dir.glob("*.pth")) if sovits_dir.is_dir() else []
    gpt = sorted(gpt_dir.glob("*.ckpt")) if gpt_dir.is_dir() else []
    return (sovits[0] if sovits else None, gpt[0] if gpt else None)


# ---------------- 读取：json / png → 原始 dict ----------------

def load_tavern_card(path: Path) -> dict:
    """读酒馆卡文件（.json 直读；.png 提取内嵌文本块），返回原始 dict。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TavernCardError(f"JSON 解析失败：{exc.__class__.__name__}: {exc}") from exc
    elif suffix == ".png":
        raw = _card_from_png(path)
    else:
        raise TavernCardError(f"不支持的文件类型 {path.suffix!r}（只认 .json / .png）")
    if not isinstance(raw, dict):
        raise TavernCardError("卡数据不是 JSON 对象")
    return raw


def _card_from_png(path: Path) -> dict:
    """扫 PNG 文本块提取卡数据：关键字 ccv3 优先、chara 兜底。"""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise TavernCardError("不是有效的 PNG 文件（签名不符）")
    chunks: dict[str, bytes] = {}
    i = 8
    while i + 12 <= len(data):
        (length,) = struct.unpack_from(">I", data, i)
        ctype = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + length]
        i += 12 + length
        if ctype == b"IEND":
            break
        if ctype == b"tEXt":
            kw, _, value = body.partition(b"\x00")
            _append_text(chunks, kw, value)
        elif ctype == b"zTXt":
            kw, _, rest = body.partition(b"\x00")
            if not rest or rest[0] != 0:            # 仅认 zlib 压缩法
                continue
            try:
                _append_text(chunks, kw, zlib.decompress(rest[1:]))
            except zlib.error:
                continue
        elif ctype == b"iTXt":
            kw, _, rest = body.partition(b"\x00")
            if len(rest) < 2:
                continue
            compressed, method = rest[0], rest[1]
            rest = rest[2:]
            if compressed:
                if method != 0:
                    continue
                try:
                    rest = zlib.decompress(rest)
                except zlib.error:
                    continue
            _, _, rest = rest.partition(b"\x00")     # language tag
            _, _, text = rest.partition(b"\x00")     # translated keyword
            _append_text(chunks, kw, text)
    for keyword in ("ccv3", "chara"):
        blob = chunks.get(keyword)
        if blob:
            return _b64_json(blob, keyword)
    raise TavernCardError("PNG 内未找到卡数据（缺 chara/ccv3 文本块）")


def _append_text(chunks: dict[str, bytes], kw: bytes, value: bytes) -> None:
    try:
        key = kw.decode("latin-1")
    except Exception:
        return
    chunks[key] = chunks.get(key, b"") + value       # 同关键字多块拼接


def _b64_json(blob: bytes, keyword: str) -> dict:
    text = bytes(c for c in blob if c not in b"\r\n \t")
    text += b"=" * (-len(text) % 4)                  # 容错缺省 padding
    try:
        decoded = base64.b64decode(text)
        return json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise TavernCardError(
            f"PNG {keyword} 块 base64/JSON 解码失败：{exc.__class__.__name__}") from exc


# ---------------- 归一：v1 顶层 / v2 / v3 data → 统一视图 ----------------

def normalize_tavern_card(raw: dict) -> dict:
    """v1（顶层字段）/ v2 / v3（data 字段）→ 统一字段视图（缺字段补空）。"""
    data = raw.get("data")
    card = data if isinstance(data, dict) else raw
    out = {}
    for key in ("name", "description", "personality", "scenario", "first_mes",
                "mes_example", "system_prompt", "post_history_instructions",
                "creator_notes", "creator", "character_version"):
        value = card.get(key)
        out[key] = value if isinstance(value, str) else ""
    greetings = card.get("alternate_greetings")
    out["alternate_greetings"] = [str(g) for g in greetings] \
        if isinstance(greetings, list) else []
    tags = card.get("tags")
    out["tags"] = [str(t) for t in tags] if isinstance(tags, list) else []
    if not out["name"].strip():
        raise TavernCardError("卡缺少 name 字段（无法确定角色名）")
    return out


# ---------------- 宏替换 ----------------

def replace_macros(text: str, char_name: str, *, user_word: str = "用户") -> str:
    """ST 宏 → 实词：{{char}}/{char} → 角色名；{{user}}/{user} → 用户。

    大小写不敏感（{{Char}}/{{USER}} 等变体一并处理）；先替换双大括号再替换单
    大括号，避免 {{user}} 先命中 {user} 规则残留右括号。
    """
    text = re.sub(r"\{\{\s*char\s*\}\}", char_name, text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{\s*user\s*\}\}", user_word, text, flags=re.IGNORECASE)
    text = re.sub(r"\{\s*char\s*\}", char_name, text, flags=re.IGNORECASE)
    text = re.sub(r"\{\s*user\s*\}", user_word, text, flags=re.IGNORECASE)
    return text


def _split_lines(text: str) -> list[str]:
    """文本 → 条目列表：逐行、去空、剥行首序号/项目符号。"""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        line = re.sub(r"^(?:\d+[.)]\s*|[-*•]\s+)", "", line).strip()
        if line:
            rows.append(line)
    return rows


# ---------------- 转包：归一卡 → .char 目录 ----------------

def build_character_card(card: dict, package_id: str) -> dict:
    """归一卡 → character_card.yaml 数据（映射规则见模块 docstring）。"""
    name = card["name"].strip()
    macro = lambda s: replace_macros(s, name)        # noqa: E731（局部便捷）

    persona_blocks: list[str] = []
    if card["description"].strip():
        persona_blocks.append(macro(card["description"]).strip())
    if card["scenario"].strip():
        persona_blocks.append("【场景】\n" + macro(card["scenario"]).strip())
    if card["system_prompt"].strip():
        persona_blocks.append("【系统提示】\n" + macro(card["system_prompt"]).strip())
    if card["first_mes"].strip():
        persona_blocks.append("【开场白（初次见面时的话，仅作语气参考）】\n"
                              + macro(card["first_mes"]).strip())
    if card["mes_example"].strip():
        persona_blocks.append(
            "【对话风格参考（学习语气与互动方式，不逐字照搬；"
            "你的输出仍须严格遵守「一行一句台词」的格式契约）】\n"
            + macro(card["mes_example"]).strip())

    tags = "、".join(card["tags"][:8]) or "无"
    creator = card["creator"].strip() or "未知"
    desc = f"酒馆卡导入（作者 {creator}；标签：{tags}）"

    out: dict = {
        "package_id": package_id,
        "schema_version": 1,
        "display_name": name,
        "name": name,
        "description": desc,
        "persona": "\n\n".join(persona_blocks),
        "taboos": list(_DEFAULT_TABOOS),
    }
    personality = _split_lines(card["personality"])
    if personality:
        out["personality"] = personality
    rules = _split_lines(card["post_history_instructions"])
    if rules:
        out["behavior_rules"] = rules
    return out


def build_relationship() -> dict:
    """中性关系脚手架（dimensions 钳制消费契约的原生形态，relationship.py §6.4）。"""
    dims = {"min": 0, "max": 100, "initial": 10, "per_reply_delta": [-3, 3]}
    return {
        "version": 1,
        "dimensions": {"affection": dict(dims), "trust": dict(dims)},
        "note": "分支按顺序首中即停（relationship.py §6.4）",
        "branches": [{"name": n, "min": lo, "max": hi, "tone_hint": hint}
                     for n, lo, hi, hint in _DEFAULT_BRANCHES],
    }


def build_tts_config() -> dict:
    """最小 fail-open 语音配置：无音色段（借用激活包候选池由导入方拷入，
    GptsovitsTts 候选池兜底接管）；剥离/守卫开关保留给 voice_postprocess
    （ST 卡的星号动作靠 strip_actions 剥离）。"""
    return {"strip_actions": True, "quote_guard": True,
            "replace_map": {"zh": {}, "ja": {}},
            "glossary": {"zh": {}, "ja": {}},
            "verbal_tics": []}


def build_manifest(card: dict, package_id: str, *,
                   sprite_note: str | None = None) -> dict:
    """包清单：tavern 溯源 + 立绘/音色现状如实标注。"""
    return {
        "package_id": package_id,
        "schema_version": 1,
        "package_version": "1.0.0",
        "spec_version": "miyako_v1.9",
        "generated_by": "client/tavern_card.py（SillyTavern 卡适配）",
        "voice_tier": 0,
        "source": {
            "tavern_card": {
                "creator": card["creator"].strip() or "未知",
                "character_version": card["character_version"].strip() or "未知",
                "tags": card["tags"],
                "note": "character_book（世界书）未导入——v1 范围外",
            },
        },
        "assets": {
            "sprite": {
                "manifest": "sprites/manifest.json",
                "note": (sprite_note or "无卡面图——使用内置占位图九张"),
            },
        },
        "fallbacks": {"unknown_emotion": "normal"},
    }


def _build_sprites(card_file: str | None) -> dict:
    if card_file:                                   # 卡面/自选图：九情绪统一映射
        mapping = {e: card_file for e in _EMOTIONS}
        dormant = card_file
    else:                                           # 无图：内置占位图逐情绪
        mapping = {e: f"{e}.png" for e in _EMOTIONS}
        dormant = "dormant.png"
    return {
        "version": 1,
        "default_outfit": "default",
        "dormant": dormant,
        "sprites": {e: {"default": {"stand": f}} for e, f in mapping.items()},
    }


def stage_tavern_package(source: Path, dest_dir: Path, *,
                         package_id: str | None = None,
                         art_path: Path | str | None = None
                         ) -> tuple[Path, str, str]:
    """酒馆卡文件 → 完整 .char 包目录（写入 dest_dir）。

    返回 (包目录, package_id, display_name)。source 为 .json 或 .png；
    立绘取图优先序：**art_path（用户自选，显式指定时不存在直接报错）**
    → PNG 卡自身卡面 → .json 的同名 .png 兄弟文件 → data/fallback 占位图。
    """
    import yaml

    source = Path(source)
    dest_dir = Path(dest_dir)
    raw = load_tavern_card(source)
    card = normalize_tavern_card(raw)
    name = card["name"].strip()
    pid = package_id or sanitize_package_id(name)
    dest_dir.mkdir(parents=True, exist_ok=True)

    art_src: Path | None = None
    art_name = ""
    if art_path is not None:
        art_src = Path(art_path)
        if not art_src.is_file():
            raise TavernCardError(f"立绘图片不存在：{art_src}")
    elif source.suffix.lower() == ".png":
        art_src = source
    else:                                            # json → 同名 png 兄弟探测
        sibling = source.with_suffix(".png")
        if sibling.is_file():
            art_src = sibling

    (dest_dir / "sprites").mkdir(parents=True, exist_ok=True)
    if art_src is not None:
        art_name = f"card{art_src.suffix.lower() or '.png'}"
        shutil.copy2(art_src, dest_dir / "sprites" / art_name)
    else:
        copied = 0
        for png in FALLBACK_SPRITES_DIR.glob("*.png"):
            shutil.copy2(png, dest_dir / "sprites" / png.name)
            copied += 1
        if copied == 0:
            logger.warning("内置占位图缺失（data/fallback/sprites）——立绘将回退内存兜底")

    (dest_dir / "character_card.yaml").write_text(
        yaml.safe_dump(build_character_card(card, pid),
                       allow_unicode=True, sort_keys=False), encoding="utf-8")
    (dest_dir / "manifest.yaml").write_text(
        yaml.safe_dump(build_manifest(card, pid,
                                      sprite_note=(f"卡面/自选图：{art_name}"
                                                   if art_src is not None else None)),
                       allow_unicode=True, sort_keys=False), encoding="utf-8")
    (dest_dir / "relationship.yaml").write_text(
        yaml.safe_dump(build_relationship(),
                       allow_unicode=True, sort_keys=False), encoding="utf-8")
    (dest_dir / "tts_config.yaml").write_text(
        yaml.safe_dump(build_tts_config(),
                       allow_unicode=True, sort_keys=False), encoding="utf-8")
    (dest_dir / "sprites" / "manifest.json").write_text(
        json.dumps(_build_sprites(art_name if art_src is not None else None),
                   ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("酒馆卡已转换：%s → %s（package_id=%s，立绘=%s）",
                source.name, dest_dir, pid,
                art_name if art_src is not None else "内置占位图")
    return dest_dir, pid, name
