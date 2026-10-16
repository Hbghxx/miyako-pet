# -*- mode: python ; coding: utf-8 -*-
"""miyako exe 发行构建（PyInstaller 多入口单目录）。

双入口共享一份 _internal（单 COLLECT 合流，避免两份运行时体积翻倍）：
  - miyako.exe        客户端（windowed：无控制台，桌面宠物本体）
  - miyako-server.exe 服务端（console：经 启动miyako.bat 以 /min 最小化拉起，
                      安全模式 bat 直接可见前台日志；崩溃留 traceback 出口）

数据面纪律（APP_ROOT，见 server/constants.py）：exe 冻结形态下 data/、config/
与 exe 并排——由 scripts/build_exe.py 组装进发行树，**不**打进 _internal
（settings/角色包必须用户可编辑可替换）。

构建：pyinstaller --noconfirm --clean --distpath <work>/dist --workpath <work>/build
           scripts/miyako_exe.spec
编排入口：scripts/build_exe.py（含发行树组装/禁入清单校验/zip）。
"""
from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# Analysis 的相对路径相对 spec 目录（scripts/）解析——仓库根经 SPECPATH 上溯
REPO = Path(SPECPATH).resolve().parent

# 优先整包收集：带二进制/数据文件的依赖（懒加载或 hook 缺失时静态分析发现不了）
# 前四组=核心链（ASR/播放/sqlite-vec 扩展），其余=可选 extras（构建机装了才进包，
# 缺席时运行时按既有 fail-open 降级，与源码安装同语义）。
COLLECT_PACKAGES = (
    "sqlite_vec",                          # sqlite 可加载扩展 vec0.dll
    "sounddevice",                         # portaudio dll（_sounddevice_data）
    "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",  # ASR
    "pyaec", "pycaw", "comtypes",          # AEC / 系统音频峰值
    "edge_tts", "miniaudio",               # edge 音色级（链外，导入兜底存在）
    "rapidocr_onnxruntime", "onnxruntime",  # 本地 OCR（闲置嗅探）
)

datas: list = []
binaries: list = []
hiddenimports: list = []
for _pkg in COLLECT_PACKAGES:
    if find_spec(_pkg) is None:
        continue
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a_client = Analysis(
    [str(REPO / "run_client.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "matplotlib", "IPython"],
    noarchive=False,
)
a_server = Analysis(
    [str(REPO / "run_server.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "matplotlib", "IPython"],
    noarchive=False,
)

pyz_client = PYZ(a_client.pure)
pyz_server = PYZ(a_server.pure)

exe_client = EXE(
    pyz_client,
    a_client.scripts,
    [],
    exclude_binaries=True,
    name="miyako",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 桌面宠物：无控制台
    disable_windowed_traceback=False,
)
exe_server = EXE(
    pyz_server,
    a_server.scripts,
    [],
    exclude_binaries=True,
    name="miyako-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # 最小化控制台：崩溃 traceback 可见可截
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe_client,
    exe_server,
    a_client.binaries,
    a_client.zipfiles,
    a_client.datas,
    a_server.binaries,
    a_server.zipfiles,
    a_server.datas,
    strip=False,
    upx=False,
    name="miyako",
)
