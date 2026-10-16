#!/usr/bin/env python3
"""依赖一致性校验 —— plan.md M-1.5 引用、M+3.5.2 验收闭环。

对 requirements.txt + pyproject.toml 的字节内容计算 sha256，与 .venv/.req_hash
（install.bat 安装成功后写入）比对：
  一致        → 退出码 0（启动链放行）
  不一致/缺失 → 退出码 1（启动 bat 据此提示用户重跑 install.bat）

用法：
  python scripts/check_req_hash.py           # 校验模式（启动 bat 调用）
  python scripts/check_req_hash.py --write   # 写入模式（install.bat 安装成功后调用）

安全说明：hash 文件路径固定派生自仓库根目录，写入前做 resolve + 包含性校验，
限制在仓库目录内，不存在路径穿越面。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = (ROOT / "requirements.txt", ROOT / "pyproject.toml")
HASH_FILE = ROOT / ".venv" / ".req_hash"


def _validated_hash_file() -> Path:
    """resolve 后校验仍位于仓库目录内，防止符号链接等造成的路径逃逸。"""
    root = ROOT.resolve()
    p = HASH_FILE.resolve()
    if p != root and root not in p.parents:
        raise RuntimeError(f"hash 文件路径越界：{p}")
    return p


def current_hash() -> str:
    h = hashlib.sha256()
    for p in MANIFESTS:
        h.update(p.read_bytes())
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="依赖清单 sha256 校验")
    parser.add_argument("--write", action="store_true",
                        help="把当前 hash 写入 .venv/.req_hash（install.bat 专用）")
    args = parser.parse_args(argv)

    for p in MANIFESTS:
        if not p.is_file():
            print(f"[FAIL] 缺少依赖清单 {p.name}，仓库不完整，请重新获取。")
            return 1

    digest = current_hash()
    if args.write:
        try:
            hash_file = _validated_hash_file()
            hash_file.parent.mkdir(parents=True, exist_ok=True)
            hash_file.write_text(digest + "\n", encoding="utf-8")
        except (OSError, RuntimeError) as exc:
            print(f"[FAIL] 依赖 hash 写入失败：{exc}")
            return 1
        print(f"[OK] 已写入依赖 hash：{digest[:16]}…")
        return 0

    if not HASH_FILE.is_file():
        print("[FAIL] 未找到 .venv/.req_hash（尚未安装或安装未完成）。")
        print("       修复指引：双击 install.bat 完成安装后再启动。")
        return 1
    recorded = HASH_FILE.read_text(encoding="utf-8").strip()
    if recorded != digest:
        print("[FAIL] 依赖清单（requirements.txt / pyproject.toml）已变化，与安装时不一致。")
        print("       修复指引：重新双击 install.bat 完成安装后再启动。")
        return 1
    print("[OK] 依赖一致性校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
