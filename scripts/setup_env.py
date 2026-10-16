#!/usr/bin/env python3
"""miyako 环境初始化（幂等）—— plan.md M-1.3。

步骤（每步打印 [OK]/[SKIP]/[FAIL]，重跑从断点继续，幂等是生命线）：
  [1/3] .venv 不存在则创建（标准库 venv.create，等价于 sys.executable -m venv）；
        残留半成品目录以 clear=True 清理重建，实现“从断点继续”
  [2/3] config/settings.yaml 与 .env 已存在则跳过不覆盖，否则从 example 复制
  [3/3] --extras a,b：在 .venv 内对每个组执行 pip install -e ".[组名]"

安全说明：--extras 的取值先过白名单（KNOWN_EXTRAS）+ 命名字符校验，之后仅用于
if/elif “选择”一条源内写死的 subprocess.run([...], shell=False) 字面量命令；
命令行参数不含任何运行期拼接（无 f-string / str() / 字符串相加），用户输入
永远不会进入命令行字符串，不存在命令注入面。
退出码：0 全部成功/跳过；1 存在 FAIL（按修复指引处理后可直接重跑）。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import venv as venv_stdlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
CONFIG_DIR = ROOT / "config"

# ---- 模块级常量：subprocess 命令行参数只取自这里与字面量 ----
ROOT_STR = os.fspath(ROOT)
VENV_PY_STR = os.fspath(VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

# 与 pyproject.toml [project.optional-dependencies] 保持一致
KNOWN_EXTRAS = ("asr", "aec", "ocr", "tts_edge", "memory", "llm_ollama")
_EXTRA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def step_create_venv() -> bool:
    print("[1/3] 虚拟环境 .venv")
    if venv_python().is_file():
        print("[SKIP] .venv 已存在，跳过创建")
        return True
    if VENV_DIR.exists():
        # 上次创建中断的半成品（有目录无解释器）：clear 重建，即“从断点继续”
        print("[....] 检测到不完整的 .venv（缺少解释器），清理后重建")
    print(f"[....] 创建虚拟环境（venv.create，with_pip=True）：{VENV_DIR}")
    try:
        venv_stdlib.create(os.fspath(VENV_DIR), with_pip=True, clear=True)
    except Exception as exc:
        print(f"[FAIL] venv 创建失败：{exc}")
        print("       修复指引：确认磁盘可写、Python>=3.10 可用后直接重跑本脚本")
        return False
    if not venv_python().is_file():
        print("[FAIL] venv 创建后未找到解释器，视为失败")
        print("       修复指引：删除 .venv 后重跑本脚本")
        return False
    print("[OK] .venv 创建完成")
    return True


def _copy_one(src: Path, dst: Path, label: str) -> bool:
    if dst.is_file():
        print(f"[SKIP] {label} 已存在，不覆盖（用户配置受保护）")
        return True
    if not src.is_file():
        print(f"[FAIL] 缺少模板 {src}，无法生成 {label}")
        print("       修复指引：从仓库恢复该 example 文件后重跑")
        return False
    shutil.copyfile(src, dst)
    print(f"[OK] 已从 example 生成 {label}")
    return True


def step_copy_configs() -> bool:
    print("[2/3] 配置文件（存在即跳过，绝不覆盖用户配置）")
    ok1 = _copy_one(CONFIG_DIR / "settings.example.yaml", CONFIG_DIR / "settings.yaml",
                    "config/settings.yaml")
    ok2 = _copy_one(CONFIG_DIR / ".env.example", ROOT / ".env", ".env（仓库根）")
    if ok1:
        # 生成结果自检：默认配置必须全 mock 且绑定 127.0.0.1:9800
        text = (CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8", errors="replace")
        if "mock" in text and "127.0.0.1" in text and "9800" in text:
            print("[OK] settings.yaml 自检通过：全 mock provider + server 127.0.0.1:9800")
        else:
            print("[FAIL] settings.yaml 自检失败：缺少 mock 组或 server 127.0.0.1:9800 段")
            print("       修复指引：检查 config/settings.example.yaml 是否完整；删除 settings.yaml 后重跑")
            ok1 = False
    return ok1 and ok2


def step_install_extras(extras: list[str]) -> bool:
    print("[3/3] 可选依赖组（extras）")
    if not extras:
        print("[SKIP] 未指定 --extras（base 依赖由 install.bat 或 pip install -r requirements.txt 安装）")
        return True
    unknown = [e for e in extras
               if e not in KNOWN_EXTRAS or not _EXTRA_NAME_RE.match(e)]
    if unknown:
        print(f"[FAIL] 未知 extras：{','.join(unknown)}（可选值：{','.join(KNOWN_EXTRAS)}）")
        return False

    if not venv_python().is_file():
        print("[FAIL] .venv 不可用，无法安装 extras")
        print("       修复指引：删除 .venv 后重跑本脚本")
        return False

    # 全新 py3.10 venv 自带 pip 23.0.1：不会把用户请求的 extra 名做 PEP 685
    # 规范化匹配（llm_ollama vs 元数据里的 llm-ollama），下划线组名会被静默跳过，
    # 导致 extras 依赖漏装却报 [OK]。先升级 venv 内 pip/setuptools/wheel 修复。
    print("[....] 升级 .venv 内 pip/setuptools/wheel（一次性，确保 extras 组名正确匹配）")
    rc0 = subprocess.run([VENV_PY_STR, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
                         cwd=ROOT_STR, shell=False).returncode
    if rc0 != 0:
        print("[....] pip 升级失败（继续安装；下划线组名在旧 pip 上可能被静默跳过）")
        print("       修复指引：检查网络后重跑；或手动执行 .venv\\Scripts\\pip install -U pip setuptools wheel")

    done: list[str] = []
    for extra in extras:
        print(f"[....] .venv 内安装 extras [{extra}]（首次联网下载，可能较慢）")
        if extra == "asr":
            rc = subprocess.run([VENV_PY_STR, "-m", "pip", "install", "-e", ".[asr]"],
                                cwd=ROOT_STR, shell=False).returncode
        elif extra == "aec":
            rc = subprocess.run([VENV_PY_STR, "-m", "pip", "install", "-e", ".[aec]"],
                                cwd=ROOT_STR, shell=False).returncode
        elif extra == "ocr":
            rc = subprocess.run([VENV_PY_STR, "-m", "pip", "install", "-e", ".[ocr]"],
                                cwd=ROOT_STR, shell=False).returncode
        elif extra == "tts_edge":
            rc = subprocess.run([VENV_PY_STR, "-m", "pip", "install", "-e", ".[tts_edge]"],
                                cwd=ROOT_STR, shell=False).returncode
        elif extra == "memory":
            rc = subprocess.run([VENV_PY_STR, "-m", "pip", "install", "-e", ".[memory]"],
                                cwd=ROOT_STR, shell=False).returncode
        elif extra == "llm_ollama":
            rc = subprocess.run([VENV_PY_STR, "-m", "pip", "install", "-e", ".[llm_ollama]"],
                                cwd=ROOT_STR, shell=False).returncode
        else:
            raise ValueError(f"extras 未过白名单：{extra!r}")

        if rc != 0:
            print(f"[FAIL] extras [{extra}] 安装失败（不阻断其余组；对应功能按 plan §4.1 降级矩阵运行）")
            print("       修复指引：检查网络后重跑本脚本并带上相同 --extras；或先跳过用 preflight 查看降级状态")
            return False
        done.append(extra)
    print(f"[OK] extras 安装完成：{','.join(done)}")
    return True


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="miyako 环境初始化（幂等，可反复运行）")
    parser.add_argument("--extras", default="",
                        help="逗号分隔的可选依赖组: asr,aec,ocr,tts_edge,memory,llm_ollama")
    args = parser.parse_args()
    extras = [e.strip() for e in args.extras.split(",") if e.strip()]

    print(f"miyako setup_env —— 项目根：{ROOT_STR}")
    results = [
        ("venv 创建", step_create_venv()),
        ("配置生成", step_copy_configs()),
        ("extras 安装", step_install_extras(extras)),
    ]
    print("-" * 56)
    for name, ok in results:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
    if all(ok for _, ok in results):
        print("全部就绪。建议下一步：python scripts/preflight.py")
        return 0
    print("存在失败项：按上方修复指引处理后重跑（幂等，已完成步骤自动 [SKIP]）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
