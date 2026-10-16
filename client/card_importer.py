"""角色包一键导入后端（2026-09-17 用户需求：托盘一键配置界面）。

把任意源文件夹的完整角色卡导入 `data/characters/{package_id}.char`：
  scan_card()   组件清单校验——必需 character_card.yaml / manifest.yaml /
                relationship.yaml / tts_config.yaml / sprites/manifest.json；
                可选 reference_audio（缺=可从供体包补拷）与 tts/ 权重（可接线）
  import_card() 拷贝 → 激活校验（validate_character_package，失败整体回滚）
                → 参考音频补拷 → 可选 TTS 权重接线（权重拷入 GPT-SoVITS
                权重目录 + 服务推理 yaml `custom:` 块改指向，原 yaml 先备份）

激活写回（settings.character.package_id）归 switcher.write_active_package_id
（单一职责：本模块只做文件面）；重启编排归 switcher.schedule_restart。
线程模型：import_card 含大目录拷贝，调用方（对话框）放工作线程执行。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from server.constants import (CHARACTERS_DIR, KNOWN_SCHEMA_VERSIONS,
                              validate_character_package)

logger = logging.getLogger(__name__)

_PACKAGE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")   # 与 constants/switcher 同一消毒规则

_REQUIRED_FILES = ("character_card.yaml", "manifest.yaml",
                   "relationship.yaml", "tts_config.yaml")


def is_valid_package_id(package_id: str) -> bool:
    return bool(_PACKAGE_ID_RE.fullmatch(package_id or ""))


def sanitize_package_id(name: str) -> str:
    """文件夹名/卡名 → 合法 package_id（小写、空白与非法字符转 -、去首尾 -）。"""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    return cleaned or "imported_card"


@dataclass
class CardScan:
    """scan_card 结果：逐项检查 + 可选组件情报。"""

    source: Path
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    package_id_hint: str = ""
    display_name: str = ""
    sovits_weights: list[Path] = field(default_factory=list)   # 相对 source
    gpt_weights: list[Path] = field(default_factory=list)
    has_reference_audio: bool = False
    has_candidates: bool = False


def scan_card(source: Path) -> CardScan:
    """组件清单校验（只读；快——纯存在性/解析检查，不遍历大文件内容）。"""
    source = Path(source)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    if not source.is_dir():
        check("源文件夹", False, "不存在或不是目录")
        return CardScan(source=source, ok=False, checks=checks)

    import yaml

    card_ok = card_id = None
    card_detail = ""
    display = ""
    card_path = source / "character_card.yaml"
    if card_path.is_file():
        try:
            card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
            if isinstance(card, dict):
                card_ok = True
                card_id = str(card.get("package_id") or "")
                display = str(card.get("display_name") or "")
            else:
                card_ok = False
                card_detail = "不是映射结构"
        except Exception as exc:
            card_ok = False
            card_detail = f"解析失败：{exc.__class__.__name__}"
    else:
        card_detail = "缺失"
    check("character_card.yaml", bool(card_ok), card_detail)
    if card_ok and not card_id:
        check("character_card.package_id", False, "缺 package_id")

    manifest_ok = False
    manifest_path = source / "manifest.yaml"
    if manifest_path.is_file():
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest_ok = isinstance(manifest, dict)
            if manifest_ok and manifest.get("schema_version") not in KNOWN_SCHEMA_VERSIONS:
                manifest_ok = False
                check("manifest.schema_version", False,
                      f"未知版本 {manifest.get('schema_version')!r}")
        except Exception as exc:
            check("manifest.yaml", False, f"解析失败：{exc.__class__.__name__}")
    check("manifest.yaml", manifest_ok, "" if manifest_ok else "缺失或非法")

    for name in _REQUIRED_FILES[2:]:                    # relationship/tts_config
        p = source / name
        ok = p.is_file()
        if ok:
            try:
                ok = isinstance(yaml.safe_load(p.read_text(encoding="utf-8")), dict)
            except Exception:
                ok = False
        check(name, ok, "" if ok else "缺失或不是映射")

    sprites_json = source / "sprites" / "manifest.json"
    check("sprites/manifest.json", sprites_json.is_file(),
          "" if sprites_json.is_file() else "缺失（立绘索引必需）")
    # （sprites 目录存在性由 sprites/manifest.json 的存在蕴含，不再单查——
    #  旧循环 `_REQUIRED_DIRS[0:]` + `d != "sprites"` 恒假，是永不执行的死代码）

    sovits = sorted((source / "tts" / "SoVITS_weights_v2").glob("*.pth")) \
        if (source / "tts" / "SoVITS_weights_v2").is_dir() else []
    gpt = sorted((source / "tts" / "GPT_weights_v2").glob("*.ckpt")) \
        if (source / "tts" / "GPT_weights_v2").is_dir() else []
    has_ref = (source / "reference_audio").is_dir()
    has_cand = (source / "reference_audio" / "candidates.yaml").is_file()
    check("reference_audio（参考音频池）", has_ref and has_cand,
          "缺失 → 可从当前激活包补拷候选池" if not (has_ref and has_cand) else "")
    if sovits:
        check("TTS 权重（SoVITS）", True, f"{len(sovits)} 个 .pth 可接线")
    if gpt:
        check("TTS 权重（GPT）", True, f"{len(gpt)} 个 .ckpt 可接线")

    required_ok = all(ok for name, ok, _ in checks
                      if name in ("character_card.yaml", "manifest.yaml",
                                  "relationship.yaml", "tts_config.yaml",
                                  "sprites/manifest.json")
                      or name == "character_card.package_id")
    return CardScan(source=source, ok=bool(card_ok and manifest_ok and required_ok),
                    checks=checks,
                    package_id_hint=sanitize_package_id(card_id or source.name),
                    display_name=display,
                    sovits_weights=[p.relative_to(source) for p in sovits],
                    gpt_weights=[p.relative_to(source) for p in gpt],
                    has_reference_audio=has_ref, has_candidates=has_cand)


@dataclass
class TtsWiring:
    """TTS 权重接线参数：源权重 → GPT-SoVITS 权重目录 → 服务推理 yaml。"""

    gptsovits_root: Path            # GPT-SoVITS 仓根（权重目录与 _setup 均由它派生）
    vits_src: Path                  # SoVITS .pth（源卡内路径）
    source_root: Path               # 源卡根（解析 vits_src 相对路径用）
    t2s_src: Path | None = None     # GPT ckpt（可选；缺省不动服务 t2s 指向）


@dataclass
class ImportResult:
    ok: bool
    target: Path | None = None
    steps: list[str] = field(default_factory=list)
    error: str = ""
    wiring: dict | None = None      # 接线结果（yaml 路径/备份/拷贝的权重）
    wiring_error: str = ""          # TTS 接线失败原因（卡保留，仅接线未完成）


def import_card(source: Path, package_id: str, *,
                characters_root: Path | None = None,
                overwrite: bool = False,
                reference_donor: Path | None = None,
                wiring: TtsWiring | None = None) -> ImportResult:
    """导入角色卡。

    失败语义分两段（2026-09-17 自查修正）：
    - 卡面阶段（拷贝/校验）失败 → **整体回滚**（含 overwrite 时先暂存旧包、
      失败原样恢复——旧卡永不因导入失败而丢失），ok=False + error 原因；
    - TTS 接线阶段失败 → **卡保留**（卡本身完好，仅接线未完成），
      ok=True + wiring_error 带原因，可修好条件后重新接线。
    """
    source = Path(source)
    if not is_valid_package_id(package_id):
        return ImportResult(False, error=f"非法 package_id：{package_id!r}")
    root = characters_root or CHARACTERS_DIR
    target = root / f"{package_id}.char"
    if target.exists():
        if not overwrite:
            return ImportResult(False, target=target,
                                error=f"目标已存在：{target}（勾选覆盖可转正/重装）")
    steps: list[str] = []
    old_pkg = target.with_name(f"{target.stem}.old-{dt.date.today().isoformat()}")
    try:
        if target.exists():                                 # overwrite 转正/重装
            if old_pkg.exists():                            # 上次残留先让位
                shutil.rmtree(old_pkg)
            target.rename(old_pkg)                          # 旧包暂存（失败可恢复）
            steps.append(f"旧包已暂存 → {old_pkg.name}")
        shutil.copytree(source, target)
        steps.append(f"已拷贝卡片 → {target}")
        if not (target / "reference_audio" / "candidates.yaml").is_file():
            donor = Path(reference_donor) if reference_donor else None
            donor_cand = (donor / "reference_audio" / "candidates.yaml"
                          if donor else None)
            if donor_cand and donor_cand.is_file():
                shutil.copytree(donor / "reference_audio" / "candidates",
                                target / "reference_audio" / "candidates",
                                dirs_exist_ok=True)
                shutil.copy2(donor_cand,
                             target / "reference_audio" / "candidates.yaml")
                steps.append("已从当前激活包补拷候选池（reference_audio）")
            else:
                steps.append("⚠️ 源卡缺参考音频池且无供体——陪玩/语音参考可能降级")
        invalid = validate_character_package(target)
        if invalid is not None:
            raise RuntimeError(f"导入后激活校验失败：{invalid}")
        steps.append("激活校验通过")
    except Exception as exc:
        # 卡面阶段失败：回滚新包 + 恢复暂存的旧包（旧卡永不因导入失败而丢失）
        shutil.rmtree(target, ignore_errors=True)
        restored = False
        if old_pkg.exists():
            try:
                old_pkg.rename(target)
                restored = True
            except OSError:
                logger.warning("旧包恢复失败（暂存于 %s）", old_pkg, exc_info=True)
        reason = f"{exc.__class__.__name__}: {exc}"
        logger.warning("角色包导入失败已回滚：%s（%s）", package_id, reason)
        hint = "（旧包已原样恢复）" if restored else (
            f"（旧包暂存于 {old_pkg.name}，未丢失）" if old_pkg.exists() else "")
        return ImportResult(False, target=target,
                            error=f"{reason}{hint}", steps=steps)
    # ---- 卡已就绪；TTS 接线失败不再回滚卡片（卡完好，仅接线未完成） ----
    wiring_info = None
    wiring_error = ""
    if wiring is not None:
        try:
            wiring_info = wire_tts_service(wiring.gptsovits_root,
                                           wiring.source_root / wiring.vits_src,
                                           (wiring.source_root / wiring.t2s_src)
                                           if wiring.t2s_src else None)
            steps.append("TTS 权重已接线（服务 yaml 已备份并改指向）")
        except Exception as exc:
            wiring_error = f"{exc.__class__.__name__}: {exc}"
            steps.append(f"⚠️ TTS 权重接线失败：{wiring_error}")
            logger.warning("TTS 权重接线失败（卡已保留）：%s", wiring_error,
                           exc_info=True)
    if old_pkg.exists():                                    # 转正成功 → 清暂存
        shutil.rmtree(old_pkg, ignore_errors=True)
    logger.info("角色包导入完成：%s（%s）", package_id, "；".join(steps))
    return ImportResult(True, target=target, steps=steps, wiring=wiring_info,
                        wiring_error=wiring_error)


def wire_tts_service(gptsovits_root: Path, vits_src: Path,
                     t2s_src: Path | None = None, *,
                     yaml_name: str = "tts_infer_miyako.yaml") -> dict:
    """TTS 权重接线：拷权重进 GPT-SoVITS 权重目录 + 服务推理 yaml 改指向。

    只改 yaml `custom:` 顶层块内的 vits/t2s 两行（v1/v2 预置块不动）；
    原 yaml 先备份为 <名>.bak-YYYYMMDD（当日首份，重复接线不覆盖首备份）。
    返回 {yaml, backup, vits, t2s}（均为实际落点路径）。
    """
    root = Path(gptsovits_root)
    sovits_dir = root / "SoVITS_weights_v2"
    gpt_dir = root / "GPT_weights_v2"
    sovits_dir.mkdir(parents=True, exist_ok=True)
    vits_dst = sovits_dir / Path(vits_src).name
    shutil.copy2(vits_src, vits_dst)
    t2s_dst = None
    if t2s_src is not None:
        gpt_dir.mkdir(parents=True, exist_ok=True)
        t2s_dst = gpt_dir / Path(t2s_src).name
        shutil.copy2(t2s_src, t2s_dst)

    yaml_path = root / "_setup" / yaml_name
    backup = yaml_path.with_name(
        f"{yaml_path.stem}.bak-{dt.date.today().isoformat()}{yaml_path.suffix}")
    if not backup.exists():
        shutil.copy2(yaml_path, backup)
    lines = yaml_path.read_text(encoding="utf-8").splitlines()
    in_custom = False
    out: list[str] = []
    replaced: set[str] = set()
    for line in lines:
        if re.match(r"^\S", line):
            in_custom = line.startswith("custom")
        m = re.match(r"^(\s+)(vits_weights_path|t2s_weights_path)\s*:.*$", line) \
            if in_custom else None
        if m:
            indent, key = m.group(1), m.group(2)
            if key == "vits_weights_path":
                out.append(f"{indent}{key}: SoVITS_weights_v2/{vits_dst.name}")
                replaced.add(key)
                continue
            if t2s_dst is not None:
                out.append(f"{indent}{key}: GPT_weights_v2/{t2s_dst.name}")
                replaced.add(key)
                continue
        out.append(line)
    yaml_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    logger.info("TTS 服务已接线：%s（vits=%s，备份=%s）",
                yaml_path.name, vits_dst.name, backup.name)
    return {"yaml": yaml_path, "backup": backup, "vits": vits_dst,
            "t2s": t2s_dst}
