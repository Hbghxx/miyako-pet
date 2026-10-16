"""全局常量与激活角色包解析的唯一事实源。

原则 10（M-CHA.1 修订）：角色资产目录一律经 ``resolve_character_package()``
解析，禁止散落硬编码包名常量（旧激活常量已退役，全仓零残留）。
原则 8/安全铁律：BIND_HOST 只允许 127.0.0.1（server/main.py 强制执行）。

resolver 语义（plan.md M-CHA.1）：
  settings.character.package_id（缺省 "miyako"，节缺失 fail-open）→
  校验 data/characters/{id}.char/manifest.yaml 存在且 schema_version 已知 →
  返回包路径；包缺失/manifest 非法 → 回退 miyako.char + 启动横幅 ⚠️（单次）。

零依赖约束：本模块不 import 任何 server/client 模块（yaml 在读取函数内 lazy
import）——全仓最底层，被双侧共同引用，禁止反向依赖。
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# PyInstaller 冻结（exe 发行）形态下的「应用根」= exe 所在目录：data/、config/
# 与 exe 并排。源码形态恒等于 REPO_ROOT（__file__ 推导），全部既有测试零影响。
APP_ROOT: Path = (Path(sys.executable).resolve().parent
                  if getattr(sys, "frozen", False) else REPO_ROOT)

# 应用层配置单文件（v1.9.1 布局；.env 只放机密）。tests 可 monkeypatch 注入。
# 冻结（exe）形态下数据面一律跟随 APP_ROOT（exe 并排的 data/、config/）。
SETTINGS_PATH = APP_ROOT / "config" / "settings.yaml"
CHARACTERS_DIR = APP_ROOT / "data" / "characters"

DEFAULT_PACKAGE_ID = "miyako"
# 激活校验已知版本集：未知版本在激活层=非法（回退 miyako）；工具/preflight 层
# 仅 ⚠️ 不阻断（M-CHA.5）——两层宽严不同是 plan 定案，勿混同。
KNOWN_SCHEMA_VERSIONS = frozenset({1})
_PACKAGE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")   # 目录名消毒：禁路径穿越

# 服务端默认端口（settings.server.port 缺省时的回落值）
SERVER_PORT_DEFAULT = 9800

# 安全铁律：服务端仅监听本地回环；读到其它值一律强制覆盖 + warning
BIND_HOST = "127.0.0.1"

logger = logging.getLogger(__name__)

_fallback_warned = False   # 启动横幅单次 flag（import 期多模块各调一次，防刷屏）


def reset_package_fallback_warn() -> None:
    """测试辅助：复位单次 ⚠️ 横幅 flag。"""
    global _fallback_warned
    _fallback_warned = False


# (mtime_ns, size) 键的文本读缓存：usage 路径解析链（每 LLM 请求/VLM 帧）会
# 反复读 settings.yaml + manifest.yaml——文件未变时免去重复 read+YAML parse。
# 键含路径本身（测试 monkeypatch SETTINGS_PATH / 写 tmp 文件即时生效）。
# ⚠️ 同 size 同 mtime 刻的重写会拿到陈旧缓存（NTFS 时间戳粒度粗）——所有
# settings 写入方必须调 invalidate_text_cache(path)（读后写一致性的契约半边）。
_TEXT_CACHE: dict[str, tuple[tuple[int, int], str]] = {}
_TEXT_CACHE_LOCK = threading.Lock()
_TEXT_CACHE_MAX = 512   # 条目上限（产品 ~4 键；防测试会话 tmp 路径无限累积）


def _read_text_cached(path: Path) -> str:
    key = str(path)
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _TEXT_CACHE_LOCK:
        hit = _TEXT_CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    text = path.read_text(encoding="utf-8")
    with _TEXT_CACHE_LOCK:
        if len(_TEXT_CACHE) >= _TEXT_CACHE_MAX:
            _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
        _TEXT_CACHE[key] = (stamp, text)
    return text


def invalidate_text_cache(path: Path | None) -> None:
    """settings 写入方的缓存失效契约：写完必调，同进程读后写立即可见。"""
    if path is None:
        return
    with _TEXT_CACHE_LOCK:
        _TEXT_CACHE.pop(str(path), None)


def load_settings(path: Path | None = None) -> dict:
    """读取 config/settings.yaml 的**全项目唯一入口**（2026-09-12 收敛，卫生批次③）。

    fail-open：缺失/损坏 → 空表缺省（各消费方再按节取默认）。client/server
    一律经本函数取 settings dict——直接 ``yaml.safe_load`` settings.yaml 在
    收敛执法测试（test_settings_single_entry）里只允许出现在本模块。
    ``path`` 仅供测试注入临时文件；缺省读模块级 SETTINGS_PATH（monkeypatch
    该常量仍生效）。
    """
    real = Path(path) if path is not None else SETTINGS_PATH
    try:
        if not real.is_file():
            return {}
        import yaml

        data = yaml.safe_load(_read_text_cached(real))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("settings.yaml 读取失败，各节按缺省处理", exc_info=True)
        return {}


def validate_character_package(pkg_dir: Path) -> str | None:
    """激活合法性校验。合法返回 None；否则返回白话错误原因（resolver/preflight 共用）。

    校验范围 = 激活最低门槛：manifest.yaml 存在、可解析为映射、schema_version
    已知。资产完整性（sprites/tts_config 等）不在此层——那是 preflight 的
    逐包体检职责（M-CHA.5），各消费模块另有 fail-open 兜底。
    """
    manifest_path = pkg_dir / "manifest.yaml"
    if not manifest_path.is_file():
        return f"manifest.yaml 不存在（{pkg_dir}）"
    try:
        import yaml

        manifest = yaml.safe_load(_read_text_cached(manifest_path))
    except Exception as exc:
        return f"manifest.yaml 解析失败（{exc.__class__.__name__}）"
    if not isinstance(manifest, dict):
        return "manifest.yaml 不是映射结构"
    if manifest.get("schema_version") not in KNOWN_SCHEMA_VERSIONS:
        return (f"未知 schema_version：{manifest.get('schema_version')!r}"
                f"（已知：{sorted(KNOWN_SCHEMA_VERSIONS)}）")
    return None


def resolve_character_package(settings: dict | None = None, *,
                              characters_root: Path | None = None) -> Path:
    """激活角色包路径唯一解析点（M-CHA.1）。

    ``settings`` 注入参数（测试必设）；None 时本地最小读取 settings.yaml。
    ``characters_root`` 仅测试注入（默认仓库 data/characters，禁在产品代码传参）。
    返回包目录（.../{package_id}.char）；消费方按相对布局自取子路径
    （character_card.yaml / tts_config.yaml / sprites/manifest.json）。
    """
    global _fallback_warned
    root = characters_root or CHARACTERS_DIR

    package_id = DEFAULT_PACKAGE_ID
    if settings is None:
        settings = load_settings()
    section = settings.get("character")
    if isinstance(section, dict):
        requested = str(section.get("package_id") or "").strip()
        if requested and _PACKAGE_ID_RE.fullmatch(requested):
            package_id = requested
        elif requested:
            logger.warning("⚠️ character.package_id 含非法字符（%r），按 %s 处理",
                           requested, DEFAULT_PACKAGE_ID)

    def banner(msg: str) -> None:
        global _fallback_warned
        if not _fallback_warned:
            _fallback_warned = True
            logger.warning("⚠️ %s", msg)

    pkg_dir = root / f"{package_id}.char"
    err = validate_character_package(pkg_dir)
    if err is None:
        return pkg_dir

    fallback_dir = root / f"{DEFAULT_PACKAGE_ID}.char"
    if package_id == DEFAULT_PACKAGE_ID:
        banner(f"激活包即缺省 miyako 但不合法（{err}）——仍按 miyako 启动，"
               f"资产缺失走各模块 fail-open")
        return fallback_dir
    fb_err = validate_character_package(fallback_dir)
    if fb_err is None:
        banner(f"激活角色包不合法（{err}）→ 回退 miyako.char")
    else:
        banner(f"激活角色包不合法（{err}）且 miyako.char 也不合法（{fb_err}）"
               f"→ 仍回退 miyako.char，资产缺失走各模块 fail-open")
    return fallback_dir


def active_package_id(settings: dict | None = None, *,
                      characters_root: Path | None = None) -> str:
    """激活角色包 id（= 目录名去 .char 后缀）。回退发生时即 miyako。

    每角色一库（M-CHA.3）的记忆库命名、托盘展示等以此为准——
    与 resolver 同一判定，禁止第二处实现。
    """
    return resolve_character_package(settings,
                                     characters_root=characters_root
                                     ).name.removesuffix(".char")
