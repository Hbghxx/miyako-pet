"""统一日志装配（2026-09-17 用户需求：server/client 全量落盘 logs/）。

此前全项目零 logging 配置：INFO 级记录被 Python 默认行为整层丢弃，
WARNING+ 仅进 stderr（server=最小化窗口关窗即失；client=pythonw 无处可去），
事后排查只能靠临时探针注入。现两入口各挂一个 RotatingFileHandler：

  server → logs/server.log（console=True 时同步镜像到前台，安全模式可见）
  client → logs/client.log（pythonw 无控制台，文件是唯一运行日志出口）
  TTS    → 维持既有 logs/tts_console.log（外部进程，run_tts_api.cmd 覆盖写）

目录策略与 crash log 同源（v1.9.4 定案）：项目内 logs/ 优先（随仓库可见），
不可写回退 %LOCALAPPDATA%\\miyako\\logs；logs/ 在 .gitignore 内。
uvicorn 自带 handler 的 logger（access/error）不向 root 传播，不会重复记录。

级别设计：root 恒 INFO（防三方库 DEBUG 刷屏）；应用包 server/client 按参数
或 MIYAKO_LOG_LEVEL 放开——文件里是应用全量，三方只留 WARNING+。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from server.constants import APP_ROOT as REPO_ROOT

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
HANDLER_MARK = "_miyako_logsetup"   # 打在本模块挂的 handler 上，幂等重挂用
_APP_PACKAGES = ("server", "client")


def logs_dir(base_root: Path | None = None) -> Path:
    """日志目录：项目内 logs/ 优先（用户要求随仓库可见）；不可写回退 LOCALAPPDATA。"""
    root = Path(base_root) if base_root is not None else REPO_ROOT
    project_logs = root / "logs"
    try:
        project_logs.mkdir(parents=True, exist_ok=True)
        probe = project_logs / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return project_logs
    except OSError:
        pass
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "miyako" / "logs"


def _resolve_level(level) -> int:
    """None → 环境变量 MIYAKO_LOG_LEVEL → INFO；接受 int 或 'debug' 等字符串。"""
    if level is None:
        level = os.environ.get("MIYAKO_LOG_LEVEL", "info")
    if isinstance(level, str):
        return int(getattr(logging, level.strip().upper(), logging.INFO))
    return int(level)


def setup_logging(app: str, *, level=None, logs_dir_=None,
                  console: bool = False,
                  max_bytes: int = _MAX_BYTES,
                  backup_count: int = _BACKUP_COUNT) -> Path | None:
    """root 挂滚动文件 handler（可选控制台镜像），返回日志文件路径；失败返回 None。

    幂等：重复调用先摘本模块上次的 handler 再挂，不产生重复记录。
    文件建不出来（磁盘/权限）→ fail-open 返回 None，运行行为不受影响。
    """
    lvl = _resolve_level(level)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for pkg in _APP_PACKAGES:
        logging.getLogger(pkg).setLevel(lvl)
    for existing in [h for h in list(root.handlers)
                     if getattr(h, HANDLER_MARK, False)]:
        root.removeHandler(existing)

    directory = Path(logs_dir_) if logs_dir_ is not None else logs_dir()
    log_file = directory / f"{app}.log"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8")
    except OSError:
        return None
    fh.setFormatter(logging.Formatter(_FORMAT))
    setattr(fh, HANDLER_MARK, True)
    root.addHandler(fh)

    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(_FORMAT))
        setattr(sh, HANDLER_MARK, True)
        root.addHandler(sh)
    return log_file
