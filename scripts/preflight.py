#!/usr/bin/env python3
"""miyako 环境体检（preflight）—— plan.md M-1.4。

十四项检查全部 lazy import：依赖未安装 / 服务不可达 → ⚠️ + 白话修复指引，绝不崩溃；
无外部服务时相关项全 ⚠️ 且退出码 0（对应功能按 plan §4.1 降级矩阵自动降级）。
唯一硬性失败：Python < 3.10 → 退出码 1。

安全说明：唯一的子进程调用（nvidia-smi 显存查询）使用源内字面量参数列表，
nvidia-smi 由系统在 PATH 上解析，无任何运行期拼接或用户输入参与命令行。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "✅", "⚠️", "❌"
PY_MIN = (3, 10)

# 角色包资产清单（M1.11 生成内容；缺失/为空仅 ⚠️ 不阻断）
_ASSET_FILES = ("manifest.yaml", "character_card.yaml", "relationship.yaml",
                "tts_config.yaml", "sprites/manifest.json")


@dataclass
class CheckResult:
    status: str        # "ok" | "warn" | "fail"
    detail: str        # 现象 / 数值
    impact: str = ""   # 影响说明（warn/fail 时展示）
    fix: str = ""      # 白话修复指引（warn/fail 时展示）


def _import_or_none(modname: str):
    try:
        return importlib.import_module(modname)
    except Exception:
        return None


def _fix_pip_extra(extra: str) -> str:
    return ('修复指引：在项目根目录执行 .venv\\Scripts\\pip install -e ".['
            + extra + ']"，或重跑 install.bat')


# ---------------------------------------------------------------- 14 项检查

def check_python() -> CheckResult:
    ver = platform.python_version()
    if sys.version_info >= PY_MIN:
        return CheckResult("ok", f"Python {ver}")
    return CheckResult("fail", f"Python {ver} 低于最低要求 {PY_MIN[0]}.{PY_MIN[1]}",
                       "依赖安装与运行都可能异常",
                       "修复指引：从 python.org 安装 Python 3.10+（安装时勾选 Add python.exe to PATH）后重跑 install.bat")


def check_gpu() -> CheckResult:
    if not shutil.which("nvidia-smi"):
        return CheckResult("warn", "未检测到 NVIDIA GPU（nvidia-smi 不可用）",
                           "ASR 将用 CPU int8 模式（较慢但可用）；TTS 默认走 Edge-TTS 不受影响",
                           "修复指引：无需处理；如有独立显卡，安装最新 NVIDIA 驱动后重跑体检")
    try:
        # 字面量命令：nvidia-smi 在 PATH 上由系统解析，无任何运行期拼接
        proc = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                              shell=False, capture_output=True, text=True, timeout=8)
        line = (proc.stdout or "").strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        return CheckResult("ok", f"{parts[0]}，显存 {parts[1] if len(parts) > 1 else '未知'}")
    except Exception:
        return CheckResult("warn", "检测到 nvidia-smi 但查询失败",
                           "GPU 不可用时 ASR 自动回退 CPU 模式，不影响启动",
                           "修复指引：更新 NVIDIA 驱动后重试；不处理也可正常使用")


def check_api_key() -> CheckResult:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return CheckResult("warn", "未找到 .env 文件",
                           "BYOK 模式需自行填 key；mock 模式不需要 key，不影响启动",
                           "修复指引：运行 python scripts/setup_env.py 自动生成 .env，然后填入你的 API key")
    keys: list[str] = []
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip().upper().endswith("API_KEY") and v.strip().strip("\"'"):
            keys.append(k.strip())
    if keys:
        return CheckResult("ok", f"已配置 {len(keys)} 个 API key：{', '.join(keys)}")
    return CheckResult("warn", ".env 存在但尚未填入任何 API key",
                       "不填也能跑（mock 模式）；真模型对话（M2）需要 key",
                       "修复指引：用记事本打开仓库根目录的 .env，填入 OpenAI 兼容 API key 后保存")


def check_gptsovits() -> CheckResult:
    try:
        with socket.socket() as s:
            s.settimeout(0.5)
            code = s.connect_ex(("127.0.0.1", 9880))
    except OSError:
        code = -1
    if code == 0:
        return CheckResult("ok", "GPT-SoVITS 探活成功（127.0.0.1:9880）")
    return CheckResult("warn", "未检测到 GPT-SoVITS 服务（127.0.0.1:9880 未响应）",
                       "不影响启动：TTS 落纯气泡（2026-09-12 定案：听错误音色不如纯文本）",
                       "修复指引：如需本地音色，先启动 GPT-SoVITS 的 API 服务（监听 9880）再重新体检")


async def _edge_dry_run() -> int:
    edge_tts = _import_or_none("edge_tts")
    if edge_tts is None:
        raise RuntimeError("edge_tts 模块不可用")
    text = "こんにちは、みやこです。よろしくねえ。"   # 20 字
    comm = edge_tts.Communicate(text, voice="ja-JP-NanamiNeural")
    total = 0
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            total += len(chunk["data"])
    return total


def check_edge_tts() -> CheckResult:
    """edge-tts 现仅服务于 tools/new_character.py 的 quick-voice 音色试听工具
    （TTS 音色链已不含 edge 级——2026-09-12 定案）；此项检查的是工具可用性。"""
    if _import_or_none("edge_tts") is None:
        return CheckResult("warn", "edge-tts 未安装",
                           "仅影响 tools/new_character.py 的 quick-voice 音色试听工具；对话 TTS 不受影响",
                           _fix_pip_extra("tts_edge"))
    try:
        size = asyncio.run(asyncio.wait_for(_edge_dry_run(), timeout=15))
        return CheckResult("ok", f"Edge-TTS 联网试听成功（20 字 → {size} 字节音频）")
    except Exception as exc:
        return CheckResult("warn", f"edge-tts 已安装但联网试听失败（{exc.__class__.__name__}）",
                           "edge-tts 依赖微软在线服务，离线环境此项必然警告",
                           "修复指引：联网后重试；长期离线可忽略本项（仅影响试听工具）")


def check_asr() -> CheckResult:
    if _import_or_none("faster_whisper") is None:
        return CheckResult("warn", "faster-whisper 未安装",
                           "语音输入不可用；文本输入不受影响",
                           _fix_pip_extra("asr"))
    hf_home = os.environ.get("HF_HOME")
    base = Path(hf_home) if hf_home else Path.home() / ".cache" / "huggingface"
    for hub in (base / "hub", base):
        if hub.is_dir():
            for child in hub.iterdir():
                low = child.name.lower()
                if "whisper" in low or "systran" in low:
                    return CheckResult("ok", f"ASR 模型缓存已存在：{child.name}")
    return CheckResult("warn", "faster-whisper 已安装，但本地尚无 ASR 模型缓存",
                       "首次语音输入时自动下载（需联网，数百 MB）；文本输入不受影响",
                       "修复指引：无需立刻处理；介意等待可提前联网跑一次语音输入完成下载")


def check_aec() -> CheckResult:
    mod = _import_or_none("webrtc_audio_processing")
    if mod is None:
        return CheckResult("warn", "webrtc-audio-processing 未安装",
                           "回声消除不可用：运行时自动降级为直通 + 播放期全丢弃，barge-in 强制关闭（§4.1）",
                           _fix_pip_extra("aec"))
    apm = None
    for factory in ("AudioProcessing", "AudioProcessingModule", "WebRtcAudioProcessing", "APM"):
        cls = getattr(mod, factory, None)
        if cls is not None:
            try:
                apm = cls()
            except Exception:
                apm = None
            break
    if apm is None:
        return CheckResult("warn", "AEC 库已安装但未找到 APM 入口类",
                           "运行时按 §4.1 自动降级：直通 + 播放期全丢弃 + barge-in 强制 false",
                           "修复指引：重装 extras aec；仍失败可忽略（降级路径不影响使用）")
    frame = bytes(320)  # 10ms @ 16kHz int16 静音帧；5 帧 = 50ms 干跑
    for attr in ("process_far_frame", "ProcessFarFrame", "process_far", "process_stream", "ProcessStream"):
        fn = getattr(apm, attr, None)
        if fn is None:
            continue
        try:
            for _ in range(5):
                fn(frame)
            return CheckResult("ok", f"AEC 库加载成功，50ms 干跑通过（接口 {attr}）")
        except TypeError:
            continue
        except Exception as exc:
            return CheckResult("warn", f"AEC 库已安装但干跑失败（{exc.__class__.__name__}）",
                               "运行时按 §4.1 自动降级：直通 + 播放期全丢弃 + barge-in 强制 false",
                               "修复指引：重装 extras aec；仍失败可忽略（降级路径不影响使用）")
    return CheckResult("ok", "AEC 库加载成功（处理接口细节在 M+1.5 aec_webrtc.py 对齐）")


def check_ocr() -> CheckResult:
    if _import_or_none("rapidocr_onnxruntime") is None:
        return CheckResult("warn", "rapidocr-onnxruntime 未安装",
                           "闲置嗅探自动禁用；手动热键直接走 VLM 路（§4.1）",
                           _fix_pip_extra("ocr"))
    return CheckResult("ok", "RapidOCR 可导入")


def check_audio_input() -> CheckResult:
    sd = _import_or_none("sounddevice")
    if sd is None:
        return CheckResult("warn", "sounddevice 未安装",
                           "语音采集不可用；文本输入不受影响",
                           "修复指引：重跑 install.bat（或 .venv\\Scripts\\pip install -r requirements.txt）；最终降级为“禁用语音输入”+托盘提示")
    np = _import_or_none("numpy")
    if np is None:
        return CheckResult("warn", "numpy 未安装，无法完成试录",
                           "语音采集不可用；文本输入不受影响",
                           "修复指引：.venv\\Scripts\\pip install -r requirements.txt")
    try:
        rec = sd.rec(int(0.2 * 16000), samplerate=16000, channels=1, dtype="int16")
        sd.wait()
        rms = float(np.sqrt(np.mean(rec.astype("float64") ** 2)))
    except Exception as exc:
        return CheckResult("warn", f"无法录音（{exc.__class__.__name__}）",
                           "无可用输入设备时语音输入自动禁用并托盘提示；文本输入不受影响",
                           "修复指引：插好麦克风/耳机麦，或在系统声音设置里把正确设备设为默认输入")
    if rms > 0:
        return CheckResult("ok", f"默认输入设备试录 200ms 正常（RMS={rms:.1f}）")
    return CheckResult("warn", "试录 200ms 全静音（设备在但没有声音）",
                       "可能是麦克风被禁用/静音，或虚拟音频设备占用了默认输入",
                       "修复指引：Windows 设置 → 系统 → 声音 → 输入，选对你的麦克风并说话看音量条是否跳动")


def check_sqlite_vec() -> CheckResult:
    mod = _import_or_none("sqlite_vec")
    if mod is None:
        return CheckResult("warn", "sqlite-vec 未安装",
                           "记忆向量检索不可用，自动降级纯关键词（BM25/LIKE），接口签名不变（§4.1）",
                           _fix_pip_extra("memory"))
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        mod.load(conn)
        ver = conn.execute("select vec_version()").fetchone()[0]
        return CheckResult("ok", f"sqlite-vec 实际加载成功（vec_version={ver}）")
    except Exception as exc:
        return CheckResult("warn", f"sqlite-vec 已安装但扩展加载失败（{exc.__class__.__name__}）",
                           "常见于本机 Python 的 sqlite3 未开启扩展加载；记忆检索自动降级纯关键词，功能不中断",
                           "修复指引：可忽略（降级可用）；需要向量检索时换用支持 load_extension 的 Python 发行版")
    finally:
        conn.close()


def check_char_assets() -> CheckResult:
    """角色包体检（M-CHA.5）：逐包枚举 data/characters/*.char。

    宽严分层：manifest 未知 schema_version 在此仅 ⚠️ 提示（不阻断）；
    激活层（server.constants resolver）才做硬校验回退。
    """
    sys.path.insert(0, os.fspath(ROOT))
    try:
        from server.constants import resolve_character_package, validate_character_package

        char_root = ROOT / "data" / "characters"
        packages = (sorted(p for p in char_root.glob("*.char") if p.is_dir())
                    if char_root.is_dir() else [])
        if not packages:
            return CheckResult("warn", "data/characters/ 下没有任何角色包",
                               "空环境无法启动角色（resolver 会回退并 ⚠️）",
                               "修复指引：跑 python tools/new_character.py --package-id miyako "
                               "--display-name 都 生成占位包，或恢复 miyako.char")
        try:
            active = resolve_character_package()
        except Exception:
            active = None
        lines: list[str] = []
        bad = 0
        for pkg in packages:
            problems: list[str] = []
            try:
                m_err = validate_character_package(pkg)
            except Exception as exc:
                m_err = f"校验异常（{exc.__class__.__name__}）"
            if m_err:
                problems.append(f"manifest：{m_err}")
            for rel in _ASSET_FILES:
                p = pkg / rel
                if not p.is_file():
                    problems.append(f"缺 {rel}")
                elif p.stat().st_size == 0:
                    problems.append(f"{rel} 为空")
            tier = None
            try:
                import yaml as _yaml
                mdata = _yaml.safe_load((pkg / "manifest.yaml").read_text(encoding="utf-8")) or {}
                tier = (mdata or {}).get("voice_tier")
            except Exception:
                tier = None
            tier_txt = f"（voice_tier={tier}）" if tier is not None else ""
            mark = pkg.name + ("（激活）" if active is not None and pkg == active else "")
            if problems:
                bad += 1
                lines.append(f"{mark}{tier_txt}：{'；'.join(problems)}")
            else:
                lines.append(f"{mark}：齐全{tier_txt}")
        summary = f"共 {len(packages)} 个角色包，" + ("全部资产齐全" if bad == 0
                                                  else f"{bad} 个存在问题")
        if bad:
            return CheckResult("warn", summary, "；".join(lines),
                               "修复指引：缺项包可删掉重跑 tools/new_character.py，"
                               "或按 preflight 提示补齐文件；激活包回退见启动日志 ⚠️")
        return CheckResult("ok", summary + "——" + "；".join(lines))
    except Exception as exc:
        return CheckResult("warn", f"角色包逐包体检异常（{exc.__class__.__name__}: {exc}）",
                           "不影响启动；resolver 会在启动时回退缺省包并 ⚠️",
                           "修复指引：把本行输出发给开发者排查")
    finally:
        try:
            sys.path.remove(os.fspath(ROOT))
        except ValueError:
            pass


def check_mock_baseline() -> CheckResult:
    sys.path.insert(0, os.fspath(ROOT))
    try:
        from server.providers import factory as _factory  # M1.7 落地后自动生效
        build = getattr(_factory, "build_llm", None)
        if build is None:
            raise AttributeError("factory.build_llm 尚未实现（M1.7 落点）")
        t0 = time.perf_counter()
        stream = build("chat").stream("延迟基线测试")
        next(iter(stream), None)
        dt_ms = (time.perf_counter() - t0) * 1000
        return CheckResult("ok", f"mock LLM 首块延迟 {dt_ms:.0f} ms（SLA ≤ 1200ms）")
    except Exception as exc:
        return CheckResult("warn", f"mock 延迟基线暂不可测：{exc}",
                           "mock provider 在 M1.7 落地；届时本项自动开始测量（延迟 SLA 见 plan §0.4）",
                           "修复指引：无需处理")
    finally:
        try:
            sys.path.remove(os.fspath(ROOT))
        except ValueError:
            pass


def check_path_ascii() -> CheckResult:
    p = os.fspath(ROOT)
    if p.isascii():
        return CheckResult("ok", f"安装路径为纯 ASCII：{p}")
    return CheckResult("warn", f"安装路径包含非 ASCII 字符：{p}",
                       "中文/全角路径可能导致 pip、venv 等工具链异常",
                       "修复指引：把整个项目移动/重新克隆到纯英文路径（例如 C:\\miyako）后重装")


def check_disk() -> CheckResult:
    free_gb = shutil.disk_usage(os.fspath(ROOT)).free / 1024 ** 3
    if free_gb < 1.0:
        return CheckResult("warn", f"磁盘剩余 {free_gb:.2f} GB（< 1GB）",
                           "模型与依赖下载数百 MB 起，磁盘写满会导致安装/下载失败",
                           "修复指引：清理磁盘，至少保留 1GB（建议 5GB 以上）")
    return CheckResult("ok", f"磁盘剩余 {free_gb:.1f} GB")


def check_logs() -> CheckResult:
    """日志落盘点（2026-09-17 全量 logging 配套）：目录策略与 crash log 同源。"""
    from server.logsetup import logs_dir

    return CheckResult(
        "ok", f"日志目录 {logs_dir()}",
        "server.log / client.log（INFO，5MB×3 滚动）；crash-*.log 滚 3 份；"
        "tts_console.log 每次启动覆盖写")


CHECKS = (
    ("Python 版本", check_python),
    ("GPU 与显存", check_gpu),
    ("API key", check_api_key),
    ("GPT-SoVITS 探活", check_gptsovits),
    ("Edge-TTS 试听", check_edge_tts),
    ("ASR 模型缓存", check_asr),
    ("AEC 自检", check_aec),
    ("OCR", check_ocr),
    ("音频输入设备", check_audio_input),
    ("sqlite-vec", check_sqlite_vec),
    ("角色包资产", check_char_assets),
    ("mock 延迟基线", check_mock_baseline),
    ("安装路径 ASCII", check_path_ascii),
    ("磁盘剩余", check_disk),
    ("日志文件", check_logs),
)


def _safe_run(fn) -> CheckResult:
    try:
        return fn()
    except Exception as exc:  # 体检自身绝不崩溃
        return CheckResult("warn", f"检查过程异常（{exc.__class__.__name__}: {exc}）",
                           "该项未能完成评估，按 ⚠️ 处理，不影响启动",
                           "修复指引：把本行输出发给开发者排查；其余功能不受影响")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 64)
    print(f"miyako 环境体检 preflight    {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"项目路径：{ROOT}")
    print("=" * 64)
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for i, (name, fn) in enumerate(CHECKS, 1):
        r = _safe_run(fn)
        counts[r.status] += 1
        mark = {"ok": OK, "warn": WARN, "fail": FAIL}[r.status]
        print(f"[{i:2d}/{len(CHECKS)}] {name}  {mark}  {r.detail}".rstrip())
        if r.status != "ok":
            if r.impact:
                print(f"        影响：{r.impact}")
            if r.fix:
                print(f"        {r.fix}")
    print("=" * 64)
    print(f"结果：{OK} {counts['ok']} 项   {WARN} {counts['warn']} 项   {FAIL} {counts['fail']} 项")
    if counts["fail"]:
        print("退出码 1：存在硬性失败项，按修复指引处理后重跑。")
        return 1
    print("退出码 0：⚠️ 项不阻断启动，对应功能按降级矩阵（plan §4.1）自动降级。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
