#!/usr/bin/env python3
"""miyako 服务端启动器（M0.4 实装）。

流程：读 config/settings.yaml → host 强制回环（非 127.0.0.1 覆盖 + warning）→
打印自检横幅（生效 provider 配置表，不含任何 key）→ uvicorn 启动。
安全模式 bat 传 --log-level debug 获得前台详细日志。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
sys.path.insert(0, str(ROOT))

import logging
import os
import uvicorn  # noqa: E402

from server.constants import BIND_HOST, SERVER_PORT_DEFAULT  # noqa: E402
from server.logsetup import setup_logging  # noqa: E402
from server.main import create_app, resolve_bind_host  # noqa: E402

PID_FILE = ROOT / "data" / "server.pid"  # 托盘「完全退出」kill server 的依据

logger = logging.getLogger(__name__)


def load_env_file(path=None) -> None:
    """加载 .env（BYOK 密钥与回落 URL 的来源；M2.2 修复：此前无人加载，.env 不生效）。

    系统环境变量优先（load_dotenv 默认不覆盖已存在变量）；.env 不存在静默跳过。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(path if path is not None else ROOT / ".env")
    except ImportError:
        logger.warning("python-dotenv 未安装：.env 不生效，密钥需用系统环境变量提供")
    except OSError:
        logger.warning(".env 读取失败，密钥走系统环境变量")


def load_settings() -> dict:
    from server.constants import load_settings as _load   # 唯一入口（卫生批次③）

    return _load()


def print_banner(cfg: dict, host: str, port: int, log_level: str,
                 log_file=None) -> None:
    """自检横幅走 logging（无窗口 pythonw 运行时同样落盘 logs/server.log）。"""
    server = cfg.get("server") or {}
    providers = cfg.get("providers") or {}
    llm = cfg.get("llm") or {}
    audio = cfg.get("audio") or {}
    roles = llm.get("roles") or {r: "inherit" for r in ("chat", "emotion", "correction", "diary")}
    info = [
        "=" * 56,
        " miyako server 启动自检",
        "=" * 56,
        f"  监听      : {host}:{port}（仅本地回环）",
        f"  proto_ver : {cfg.get('proto_ver', 1)}",
        f"  log-level : {log_level}",
        f"  providers : llm={llm.get('provider', 'mock')} tts={providers.get('tts', 'mock')} "
        f"asr={providers.get('asr', 'mock')} vlm={providers.get('vlm', 'mock')}",
        f"  llm.roles : {roles}",
        f"  audio     : aec={audio.get('aec', 'none')} barge_in={audio.get('barge_in', False)}",
    ]
    if log_file is not None:
        info.append(f"  日志      : {log_file}（INFO，5MB×3 滚动）")
    info.append("  密钥      : 不在此显示（见 .env）")
    info.append("-" * 56)
    logger.info("\n%s", "\n".join(info))


def pid_alive(pid: int) -> bool:
    """跨平台进程探活（只读，绝不误杀——Windows 上 os.kill(pid,0) 语义危险故分流）。"""
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def server_already_running(pid_path=None) -> bool:
    """M2.6⑤：读 data/server.pid 探活；活进程 → True（调用方静默 exit 0）。

    重复双击启动脚本时不再拉起冲突副本（客户端侧由命名互斥量拦截，
    服务端侧由此处兜底——两条防线互不替代）。
    """
    path = pid_path or PID_FILE
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except Exception:
        return False            # 文件缺失/损坏 → 视为无活进程
    if pid == os.getpid():
        return False            # 自己残留的 pid（理论不可达，防御）
    return pid_alive(pid)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="miyako server")
    parser.add_argument("--log-level", default="info",
                        choices=("debug", "info", "warning", "error"))
    args = parser.parse_args()

    if server_already_running():
        return 0                # M2.6⑤：已有活 server → 静默退出（不拉冲突副本）

    # 日志装配先于一切 provider 装配；级别随 --log-level（安全模式 debug 真正落到应用层）
    log_file = setup_logging("server", level=args.log_level, console=True)

    load_env_file()                                     # .env 先于一切 provider 装配
    cfg = load_settings()
    server_cfg = cfg.get("server") or {}
    host = resolve_bind_host(str(server_cfg.get("host", BIND_HOST)))
    port = int(server_cfg.get("port", SERVER_PORT_DEFAULT))
    print_banner(cfg, host, port, args.log_level, log_file)
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        logger.warning("pid 文件写入失败：托盘「完全退出」将无法终止本进程")
    try:
        # log_config=None：uvicorn 缺省 formatter 在 pythonw（stdout=None）下
        # dictConfig 直接抛 ValueError（2026-09-17 无窗口启动事故）；置空后
        # uvicorn 日志传播到 root，由 logsetup 统一落盘/镜像
        uvicorn.run(create_app(cfg), host=host, port=port,
                    log_level=args.log_level, log_config=None)
    except Exception:
        # uvicorn 级异常原先只进控制台窗口（最小化=不可见，2026-09-17 03:0x
        # server 无声退出事故：server.log 零痕迹）——落盘一份再抛
        logging.getLogger("run_server").exception("uvicorn 异常退出")
        raise
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
