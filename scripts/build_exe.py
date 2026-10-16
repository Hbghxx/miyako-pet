#!/usr/bin/env python3
"""exe 发行树组装 + 禁入清单校验 + zip（PyInstaller 本体由外部命令执行）。

完整构建两步：
  1. PyInstaller（仓库根执行，产物落 build/exe/dist/miyako/）：
       .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean ^
            --distpath build\\exe\\dist --workpath build\\exe\\build ^
            scripts\\miyako_exe.spec
  2. 组装发行树（本脚本；可 --zip 打包）：
       python scripts/build_exe.py [--out <dir>] [--skip-build 时复用产物] [--zip] [--force]

发行树布局（exe 冻结形态，APP_ROOT=exe 并排，见 server/constants.py）：
  <out>/miyako.exe  miyako-server.exe  _internal/   ← COLLECT 产物（两入口共享运行时）
  <out>/VERSION                                     ← updater 读 APP_ROOT/VERSION
  <out>/data/fallback data/characters/miyako.char data/memory/.gitkeep
  <out>/config/settings.example.yaml config/.env.example
  <out>/启动miyako.bat 启动miyako(安全模式).bat README.txt 使用指南.md

安全纪律（与 make_release.py 同源）：
  - settings.yaml / .env / memory.db / server.pid / server.token **绝不进树**
    （settings.yaml 由首启 bat 从 example 复制）；组装后强制扫描，命中即终止；
  - miyako.char 排除 reference_audio/dataset（训练语料 608MB，推理只需 candidates）；
  - bat 全部 CRLF（LF-only bat 会被 cmd 按读块劈碎——历史实测坑，写即规范化）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECT_DIR = ROOT / "build" / "exe" / "dist" / "miyako"
DEFAULT_OUT = ROOT.parent / "miyako_share"
BAT_CRLF = "\r\n".join

# ---------------- 发行树静态内容（bat 模板；console 文案 ASCII，文件名可中文） ----------------

BAT_LAUNCH = """\
@echo off
rem ============================================================
rem  miyako launcher (exe distribution)
rem  Console messages are ASCII on purpose (chcp 65001 read-block
rem  boundary). Chinese guide: 使用指南.md / README.txt
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ---- first run: bootstrap config from the example ----
if not exist "config\\settings.yaml" (
    if exist "config\\settings.example.yaml" (
        copy /y "config\\settings.example.yaml" "config\\settings.yaml" >nul
        echo [INFO] config\\settings.yaml created from example.
    )
)
if not exist ".env" (
    if exist "config\\.env.example" (
        copy /y "config\\.env.example" ".env" >nul
    )
)
if not exist "data\\memory" mkdir "data\\memory"
if not exist "logs" mkdir "logs"

rem ---- optional TTS voice package (sibling folder, trained voice) ----
rem Without it miyako stays text-only by design (voice-consistency-first).
set "TTS_DIR=%~dp0..\\GPT-SoVITS-miyako-share"
if exist "%TTS_DIR%\\试听miyako.bat" (
    start "miyako-tts" /min /D "%TTS_DIR%" "%TTS_DIR%\\试听miyako.bat"
) else (
    echo [WARN] Voice package GPT-SoVITS-miyako-share not found next to
    echo        this folder. miyako runs text-only; see 使用指南.md.
)

rem ---- server first (minimized console; full log at logs\\server.log) ----
start "miyako-server" /min "miyako-server.exe"
ping -n 3 127.0.0.1 >nul
rem ---- then the pet itself ----
start "" "miyako.exe"
echo [OK] miyako started. To quit fully use the tray icon menu.
pause
exit /b 0
"""

BAT_SAFE = """\
@echo off
rem ============================================================
rem  miyako launcher - SAFE MODE (verbose server console)
rem  For diagnosis when normal start misbehaves. Chinese guide:
rem  使用指南.md
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "config\\settings.yaml" (
    if exist "config\\settings.example.yaml" (
        copy /y "config\\settings.example.yaml" "config\\settings.yaml" >nul
    )
)
if not exist "data\\memory" mkdir "data\\memory"
if not exist "logs" mkdir "logs"

echo [safe mode] starting server with debug console (keep it open)...
start "miyako-server (safe mode)" miyako-server.exe --log-level debug
ping -n 3 127.0.0.1 >nul
start "" "miyako.exe"
echo [OK] server runs in a visible window with debug logs.
pause
exit /b 0
"""

README_TXT = """\
miyako - 桌面本地 AI 陪伴角色（exe 发行版）
================================================

启动
  双击「启动miyako.bat」。首次启动会自动从 config/settings.example.yaml
  生成 config/settings.yaml（改配置改这个文件）。
  完全退出：托盘图标右键 → 退出（可选同时关闭语音服务）。

安全模式
  启动异常时用「启动miyako(安全模式).bat」：服务端 debug 日志前台可见。

语音
  - 把语音包文件夹 GPT-SoVITS-miyako-share 与本文件夹**并排**放置，
    启动 bat 会自动探测并拉起自训音色服务（详见 使用指南.md）；
  - 没有语音包时 miyako 保持纯文本（设计取舍：宁可不出声也不出错误音色）；
  - 云端克隆音色（可选）：config/settings.yaml 填 providers.tts_cloudclone，
    key 写在 .env（BYOK，不配置不发生）。

LLM / 大模型
  config/settings.yaml 填 providers.llm（OpenAI 兼容端点 + BYOK），
  key 写 .env。不配置时一切以 mock 形态运行（界面可跑，回复为占位）。

目录
  data/characters/   角色包（自带 九條都）
  data/memory/       记忆库（隐私数据，勿外传）
  config/            配置（settings.yaml 首启生成）
  logs/              运行日志（client.log / server.log，排障先看这里）
"""

GUIDE_MD = """\
# miyako 使用指南（exe 发行版）

## 一、启动

1. 解压/放置本文件夹到**任意中文或英文路径**（不要放需要管理员权限的目录）。
2. 双击 `启动miyako.bat`：首次启动自动生成 `config/settings.yaml`，随后桌面
   出现角色立绘、输入栏与托盘图标。
3. 完全退出：托盘右键 →「完全退出（同时关闭 TTS）」。

> 没有配置任何 LLM 时一切以 mock 形态运行：界面能跑，回复是占位文本。
> 想让她真正说话，见下节「三、接大模型（BYOK）」。

## 二、语音包（自训音色，可选）

把 `GPT-SoVITS-miyako-share` 文件夹放在**与本文件夹同级**（并排）：

```
某目录\\
├─ miyako_share/            ← 本文件夹
└─ GPT-SoVITS-miyako-share/ ← 语音包（含 安装环境.bat 一键装环境）
```

- 语音包首次使用先双击包内 `安装环境.bat`（自动装 conda + torch + 推理依赖）；
- 之后 `启动miyako.bat` 会自动探测并最小化拉起语音服务，出声即自训音色；
- 语音服务没启动时 miyako **保持纯文本**（设计取舍：不出错误音色）。

## 三、接大模型（BYOK，key 只存本机 .env）

1. 打开 `config/settings.yaml`（首启后才有），找到 `llm:` 节：
   ```yaml
   llm:
     provider: api
     api:
       base_url: https://open.bigmodel.cn/api/paas/v4   # OpenAI 兼容端点
       model: glm-4-flash
       api_key_env: MIYAKO_LLM_API_KEY                  # key 从 .env 读
   ```
2. 打开本文件夹的 `.env`（首启自动从 `config/.env.example` 生成），加一行：
   `MIYAKO_LLM_API_KEY=你的key`
3. 重启 `启动miyako.bat` 生效。key 只保存在本机 .env，不做任何上传。

## 四、语音识别（麦克风说话）

`providers.asr: whisper`（发行默认）时首次用麦克风会自动下载
faster-whisper base 模型（~75MB，存本机缓存，之后离线可用）。

## 五、常见问题

| 现象 | 处理 |
| --- | --- |
| 双击启动没反应 | 先跑「启动miyako(安全模式).bat」看服务端报错；或看 `logs/` |
| 说话没声音 | 语音包未就绪（纯文本态）；按上节放好并先跑包内 `安装环境.bat` |
| 想换角色 | 把角色包文件夹放进 `data/characters/`，托盘 → 切换角色 |
| 想调整立绘/气泡 | 托盘 → 设置 / 立绘 菜单实时调整 |
| 杀软报误报 | exe 未做签名，添加信任目录即可（源码形态可查发布仓库） |

隐私：记忆库（data/memory/）与 .env 全在本机；识屏/摄像头功能默认关闭，
开启前有隐私同意框。
"""


def _write_bat(path: Path, text: str) -> None:
    """bat 必须 CRLF（LF-only bat 被 cmd 按读块劈碎——历史实测坑）；UTF-8 无 BOM。"""
    data = BAT_CRLF(text.splitlines()).encode("utf-8") + b"\r\n"
    path.write_bytes(data)


def _forbidden_guard(target: Path) -> None:
    """禁入清单强制校验（make_release 同款纪律：命中即终止，不静默剔除）。"""
    forbidden_names = {"settings.yaml", ".env", "memory.db", "server.pid",
                       "server.token"}
    hits: list[Path] = []
    for p in target.rglob("*"):
        if p.is_file() and p.name in forbidden_names:
            hits.append(p)
    mem = target / "data" / "memory"
    if mem.is_dir():
        for p in mem.rglob("*"):
            if p.name != ".gitkeep":
                hits.append(p)
    if hits:
        raise SystemExit("禁入清单命中，终止：\n" +
                         "\n".join(str(h) for h in hits))


def _copy_char(src: Path, dst: Path) -> None:
    """角色包整拷（含 sprites/audiofile/reference_audio 版权物料——share 形态
    用户显式豁免），**排除 reference_audio/dataset**（训练语料 608MB，推理不用）。"""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("dataset", "__pycache__", "*- 副本*"))


def assemble(out: Path, *, force: bool) -> None:
    repo_fallback = ROOT / "data" / "fallback"
    char_src = ROOT / "data" / "characters" / "miyako.char"
    if not char_src.is_dir():
        raise SystemExit(f"角色包缺失：{char_src}")

    if not (COLLECT_DIR / "miyako.exe").is_file():
        raise SystemExit(
            f"COLLECT 产物缺失：{COLLECT_DIR}\n"
            "先执行 PyInstaller（命令见本文件头部说明）")
    if not (COLLECT_DIR / "miyako-server.exe").is_file():
        raise SystemExit(f"server 入口缺失：{COLLECT_DIR / 'miyako-server.exe'}")

    if out.exists() and any(out.iterdir()):
        if not force:
            raise SystemExit(f"目标目录非空（{out}）；确认覆盖请加 --force")
        for child in out.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    out.mkdir(parents=True, exist_ok=True)

    # exe + _internal（两入口共享运行时）
    for item in COLLECT_DIR.iterdir():
        (shutil.copytree if item.is_dir() else shutil.copy2)(
            item, out / item.name)

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    (out / "VERSION").write_text(version + "\n", encoding="utf-8")

    # 数据面（APP_ROOT 并排）
    shutil.copytree(repo_fallback, out / "data" / "fallback", dirs_exist_ok=True)
    _copy_char(char_src, out / "data" / "characters" / "miyako.char")
    (out / "data" / "memory").mkdir(parents=True, exist_ok=True)
    (out / "data" / "memory" / ".gitkeep").write_text("", encoding="utf-8")

    cfg_out = out / "config"
    cfg_out.mkdir(exist_ok=True)
    example = (ROOT / "config" / "settings.example.yaml").read_text(encoding="utf-8")
    # share 形态默认接真实引擎（源码形态 install.bat 装全 extras 同语义）；
    # mock→gptsovits/whisper 精确锚点替换（锚点缺失宁可报错不静默）
    anchor_tts = "providers:\n  tts: mock"
    anchor_asr = "  asr: mock"
    if anchor_tts not in example or anchor_asr not in example:
        raise SystemExit("settings.example.yaml 锚点缺失（tts/asr 默认行已变），"
                         "请同步更新 build_exe.py 的发行默认")
    example = example.replace(anchor_tts, "providers:\n  tts: gptsovits", 1)
    example = example.replace(anchor_asr, "  asr: whisper", 1)
    (cfg_out / "settings.example.yaml").write_text(example, encoding="utf-8")
    env_example = ROOT / "config" / ".env.example"
    if env_example.is_file():
        shutil.copy2(env_example, cfg_out / ".env.example")

    # bat（CRLF 硬性）+ 文档
    _write_bat(out / "启动miyako.bat", BAT_LAUNCH)
    _write_bat(out / "启动miyako(安全模式).bat", BAT_SAFE)
    (out / "README.txt").write_text(README_TXT, encoding="utf-8")
    (out / "使用指南.md").write_text(GUIDE_MD, encoding="utf-8")

    _forbidden_guard(out)
    size_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) // 2**20
    print(f"[OK] 发行树组装完成：{out}（约 {size_mb} MB，版本 {version}）")


def make_zip(out: Path) -> Path:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    zip_path = out.parent / f"miyako_v{version}-exe.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                zf.write(f, str(Path(out.name) / f.relative_to(out)))
        # UTF-8 语言标志位：中文文件名不乱码（新 CPython 对 ASCII 名按规范
        # 重置标志，close 前对 infolist 统一补齐——make_release 同款纪律）
        for info in zf.infolist():
            info.flag_bits |= 0x800
    print(f"[OK] zip：{zip_path}（{zip_path.stat().st_size // 2**20} MB）")
    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser(description="miyako exe 发行树组装")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"发行树目录（默认 {DEFAULT_OUT}）")
    ap.add_argument("--zip", action="store_true", help="组装后打 zip 到上级目录")
    ap.add_argument("--force", action="store_true", help="目标目录非空时清空重装")
    args = ap.parse_args()
    assemble(args.out, force=args.force)
    if args.zip:
        make_zip(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
