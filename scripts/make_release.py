#!/usr/bin/env python3
"""发布包构建（M+3.5.1）—— miyako_v{ver}.zip + release_notes.md 模板。

四条硬性要求（plan.md M+3.5.1 定案）：
  ① 内容面：剔除 tests/tools/docs（白名单制，data/characters 整目录不进包——
     版权物料零风险）；**排除清单强制校验**——memory.db（含 data/memory/ 整
     目录）、config/settings.yaml、.env、.venv、data/server.pid、data/server.token
     任一出现在待打包清单 → 立即报错终止（不做"剔除后继续"：静默剔除漏一个
     就是用户数据/机密泄露）；
  ② bat 全部校验 CRLF + UTF-8 无 BOM（记事本/中文路径友好）；
  ③ zip 全部条目 `zf.flag_bits |= 0x800`（UTF-8 语言标志位，中文文件名不乱码；
     新 CPython 写 ASCII 名会按规范重置该标志，故统一在 close 前对 infolist
     补齐，读侧以中央目录为准）；
  ④ 包名从 VERSION 取：miyako_v{ver}.zip；同时产出 release_notes.md 模板。

用法：
  python scripts/make_release.py               # 仓库根构建 → dist/
  python scripts/make_release.py -o <dir>      # 指定输出目录

安全说明：构建只读仓库、只写输出目录；EXCLUDE_PATTERNS（M-CHA.3 单一事实
源，与 .gitignore 同步维护、test_character_isolation 执法一致）+ INCLUDE
白名单双保险——白名单外的顶层内容一律不进包。
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 发布包排除清单（fnmatch 风格相对路径；M-CHA.3 单一事实源，「!」前缀=例外保留）
EXCLUDE_PATTERNS: tuple[str, ...] = (
    # 运行时产物与机密
    "data/server.pid",
    "data/server.token",
    "config/settings.yaml",
    ".env",
    ".venv/",
    "__pycache__/",
    # 每角色记忆库（M-CHA.3：用户隐私数据）
    "data/memory.db*",
    "data/memory/*",
    "!data/memory/.gitkeep",
    # 角色包游戏版权物料（产线接入 v1.9）
    "data/characters/*/sprites/*.png",
    "data/characters/*/audiofile/*",
    "data/characters/*/reference_audio/*",
)

# 🔑 强制校验清单（plan M+3.5.1① + 用户扩充：server.pid/server.token/memory 整目录）
# 目录式条目按前缀匹配，文件式按精确/fnmatch 匹配。
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "data/memory/",
    ".venv/",
    "data/tmp/",
)
FORBIDDEN_NAMES: tuple[str, ...] = (
    "data/memory.db",
    "config/settings.yaml",
    ".env",
    "data/server.pid",
    "data/server.token",
)

# 内容白名单：目录根（递归，内部仍过排除清单）+ 顶层文件。
# tests/tools/docs 明确剔除（plan M+3.5.1①）；config 只带 example/.env.example
# （本地 settings.yaml/.env 在 FORBIDDEN 强校验清单内）；data/characters 整目录
# 不进包（版权物料零风险，缺角色资产时 sprite 逐级回退 data/fallback）。
INCLUDE_DIRS: tuple[str, ...] = ("client", "server", "scripts", "config",
                                 "data/fallback")
INCLUDE_FILES: tuple[str, ...] = (
    "install.bat",
    "启动miyako.bat",
    "启动miyako(安全模式).bat",
    "卸载.bat",
    "README.txt",
    "requirements.txt",
    "pyproject.toml",
    "VERSION",
    "run_client.py",
    "run_server.py",
)


def _is_excluded(rel: str) -> bool:
    """rel（posix 相对路径）是否命中排除清单或通用编译产物。"""
    parts = Path(rel).parts
    if "__pycache__" in parts or rel.endswith((".pyc", ".pyo")):
        return True
    rel_slash = rel + "/"
    for pattern in EXCLUDE_PATTERNS:
        if pattern.startswith("!"):
            continue
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel_slash, pattern):
            return True
    return False


def collect_entries(root: Path) -> list[str]:
    """按白名单收集包内相对路径（posix，排序确定）。"""
    entries: set[str] = set()
    for dir_name in INCLUDE_DIRS:
        base = root / dir_name
        if not base.is_dir():
            continue                      # 可选目录（如 data/fallback）缺失不致命
        for path in base.rglob("*"):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                if not _is_excluded(rel):
                    entries.add(rel)
    for name in INCLUDE_FILES:
        if (root / name).is_file():
            entries.add(name)
    return sorted(entries)


def _forbidden_hits(entries: list[str]) -> list[str]:
    hits: list[str] = []
    for rel in entries:
        if rel in FORBIDDEN_NAMES or fnmatch.fnmatch(rel, "data/memory.db*"):
            hits.append(rel)
            continue
        for prefix in FORBIDDEN_PREFIXES:
            if rel.startswith(prefix):
                hits.append(rel)
                break
    return hits


def validate_bats(root: Path, names: list[str]) -> None:
    """② bat 校验：UTF-8 无 BOM + 严格 CRLF（BOM/裸 LF → 报错终止）。"""
    for name in names:
        if not name.endswith(".bat"):
            continue
        data = (root / name).read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            raise SystemExit(f"[FAIL] {name} 带 UTF-8 BOM（要求无 BOM）。")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SystemExit(f"[FAIL] {name} 不是合法 UTF-8：{exc}") from exc
        if b"\r\n" not in data:
            raise SystemExit(f"[FAIL] {name} 无 CRLF 行尾（要求 CRLF）。")
        if b"\n" in data.replace(b"\r\n", b""):
            raise SystemExit(f"[FAIL] {name} 存在裸 LF 行尾（要求全部 CRLF）。")


def _read_version(root: Path) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise SystemExit("[FAIL] VERSION 文件为空。")
    return version


def build_release(root: Path | None = None,
                  out_dir: Path | None = None) -> tuple[Path, Path]:
    """构建发布 zip + release_notes.md 模板。返回 (zip 路径, notes 路径)。"""
    root = (root or ROOT).resolve()
    out_dir = (out_dir or root / "dist").resolve()
    version = _read_version(root)

    entries = collect_entries(root)
    hits = _forbidden_hits(entries)
    if hits:
        raise SystemExit(
            "[FAIL] 排除清单强制校验失败——以下禁止内容出现在待打包清单，"
            "立即终止（不产出半截包）：\n  " + "\n  ".join(hits))
    if not entries:
        raise SystemExit("[FAIL] 白名单收集结果为空，仓库不完整。")

    bat_names = [e for e in entries if e.endswith(".bat")]
    if not bat_names:
        raise SystemExit("[FAIL] 包内没有任何 bat（分发形态不完整）。")
    validate_bats(root, bat_names)

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"miyako_v{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in entries:
            info = zipfile.ZipInfo(rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, (root / rel).read_bytes())
        # UTF-8 语言标志统一在中央目录补齐（新 CPython 写 ASCII 名会按规范重置）
        for info in zf.infolist():
            info.flag_bits |= 0x800

    notes_path = out_dir / "release_notes.md"
    notes_path.write_text(
        f"# miyako v{version} 更新说明\n\n"
        "<!-- 模板：发布前替换以下占位段落 -->\n\n"
        "## 亮点\n- （填写本次版本的代表性新能力）\n\n"
        "## 修复\n- （填写用户可感知的缺陷修复）\n\n"
        "## 已知问题\n- AEC 外放打断的真机闭环仍在迭代（遗留⑦）；"
        "如遇误打断可在 设置→参数调整→音频 关闭 barge_in，其余功能不受影响。\n\n"
        "## 升级方法\n"
        "1. 覆盖解压到安装目录（**data 文件夹勿删**——记忆/关系/配置都在里面）\n"
        "2. 重新运行一次 install.bat（幂等，补齐依赖）\n"
        "3. 双击 启动miyako.bat\n",
        encoding="utf-8")

    print(f"[OK] 发布包已生成：{zip_path}（{len(entries)} 个文件）")
    print(f"[OK] 更新说明模板：{notes_path}")
    return zip_path, notes_path


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="miyako 发布包构建（M+3.5.1）")
    parser.add_argument("-o", "--out", default=None, help="输出目录（默认 dist/）")
    parser.add_argument("--root", default=None,
                        help="仓库根（默认脚本上级目录；测试注入用）")
    args = parser.parse_args(argv)
    try:
        build_release(Path(args.root) if args.root else None,
                      Path(args.out) if args.out else None)
    except SystemExit as exc:
        if isinstance(exc.code, str):        # raise SystemExit("消息") = 构建失败
            print(exc.code, file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
