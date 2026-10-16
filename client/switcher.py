"""角色切换编排（M-CHA.4）—— 重启式切换的纯逻辑核心 + 进程管理。

时序（plan.md M-CHA.4，顺序固定）：
  校验目标包 →（失败拒绝：不写 settings + 托盘 ⚠️）→ 用户确认 →
  interrupt（打断当前对话）→ winddown 等后台评估落库（≤3s 超时强制）→
  写 settings.character.package_id → 复用「完全退出」进程管理重启双端。

架构注记：orchestrator 在 server 进程，本模块在 client 进程——「等后台评估
落库」以 winddown 时间窗实现（Interrupt 已发、窗口内服务端完成收尾写库）。
M+1/M+2 的评估落库须在 3s 窗口内完成（plan 降级矩阵 SLA）；M+1 落地后可把
is_settled 换成可观测谓词提前收工，签名不变。

形态铁律：重启式切换——本模块不做任何运行时热切换（不拆 orchestrator、
不重建 TTS 锁/句柄）；互斥量保持应用级 miyako_single_instance。
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from server.constants import (APP_ROOT, CHARACTERS_DIR, SETTINGS_PATH,
                              validate_character_package)
from server.persona import display_name_from_package

logger = logging.getLogger(__name__)

REPO_ROOT = APP_ROOT
LAUNCHER_BAT = REPO_ROOT / "启动miyako.bat"

# 「等后台评估落库」窗口上限（plan M-CHA.4：≤3s 超时强制）
SWITCH_DRAIN_TIMEOUT_S = 3.0

_PACKAGE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")   # 与 constants 同一消毒规则


@dataclass(frozen=True)
class PackageEntry:
    """切换子菜单条目：包 id + 展示名。"""

    package_id: str
    display_name: str


def list_character_packages(characters_root: Path | None = None) -> list[PackageEntry]:
    """枚举 data/characters/*.char 并逐包校验（M-CHA.4 子菜单数据源）。

    只返回激活校验通过的包（坏包进菜单即违背「目标包校验失败拒绝切换」；
    坏包由 preflight 逐包体检暴露）。按包 id 排序保证菜单顺序稳定。
    """
    root = characters_root or CHARACTERS_DIR
    entries: list[PackageEntry] = []
    if not root.is_dir():
        return entries
    for pkg_dir in sorted(root.glob("*.char")):
        if not pkg_dir.is_dir():
            continue
        if validate_character_package(pkg_dir) is not None:
            logger.warning("切换菜单跳过不合法角色包：%s", pkg_dir.name)
            continue
        entries.append(PackageEntry(package_id=pkg_dir.name.removesuffix(".char"),
                                    display_name=display_name_from_package(pkg_dir)))
    return entries


def write_active_package_id(package_id: str,
                            settings_path: Path | None = None) -> None:
    """把激活角色写入 settings.yaml 的 character.package_id（M-CHA.4）。

    行级改写保注释（settings.yaml 含大量说明注释，整体 safe_dump 会全丢）；
    节/键缺失则追加。调用方须已通过 validate_character_package 校验——
    本函数只做 id 消毒兜底，非法 id 直接拒绝（不写）。
    """
    if not _PACKAGE_ID_RE.fullmatch(package_id):
        raise ValueError(f"非法 package_id：{package_id!r}")
    path = settings_path or SETTINGS_PATH
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    out: list[str] = []
    in_character = False
    replaced = False
    section_exists = False
    for line in text.splitlines():
        if re.match(r"^character\s*:", line):
            in_character = True
            section_exists = True
            out.append(line)
            continue
        if re.match(r"^\S", line):        # 下一个顶层节
            in_character = False
        m = (re.match(r"^(\s+)package_id\s*:[^#]*(#.*)?$", line)
             if in_character else None)
        if m:
            comment = (m.group(2) or "").rstrip()
            out.append(f"{m.group(1)}package_id: {package_id}"
                       + (f"    {comment}" if comment else ""))
            replaced = True
            continue
        out.append(line)
    if not replaced:
        addition = f"  package_id: {package_id}"
        if section_exists:
            for i in range(len(out) - 1, -1, -1):
                if re.match(r"^character\s*:", out[i]):
                    out.insert(i + 1, addition)
                    break
        else:
            if out and out[-1].strip():
                out.append("")
            out += ["character:", addition]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    from server.constants import invalidate_text_cache

    invalidate_text_cache(path)   # 读缓存失效契约：读后写立即可见
    logger.info("激活角色已写入 %s：%s", path.name, package_id)


def winddown(timeout_s: float = SWITCH_DRAIN_TIMEOUT_S, *,
             is_settled: Optional[Callable[[], bool]] = None,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> bool:
    """切换前收尾等待：interrupt 后给服务端「评估落库」的时间窗（≤3s 强制）。

    is_settled 注入谓词（M+1 落地评估完成信号后传入）→ 提前收工返回 True；
    缺省等满窗口（保守——窗口本身即 plan 降级矩阵的落库 SLA）。
    返回 False = 超时强制（不阻塞切换，磁盘上已 commit 的事务不受进程退出影响）。
    """
    deadline = clock() + timeout_s
    while True:
        if is_settled is not None and is_settled():
            return True
        if clock() >= deadline:
            return False
        sleep(min(0.05, max(0.0, deadline - clock())))


@dataclass(frozen=True)
class SwitchSteps:
    """perform_switch 的副作用注入口（Qt/WS/文件系统全部在调用方装配）。"""

    is_valid: Callable[[str], bool]                 # 目标包激活校验
    confirm: Callable[[], bool]                     # 确认框（提示打断+重启）
    interrupt: Callable[[], None]                   # 发协议 Interrupt
    write_settings: Callable[[str], None]           # 写激活角色
    restart: Callable[[], None]                     # 重启双端（复用完全退出）
    on_reject: Optional[Callable[[str], None]] = None   # 坏包拒绝（托盘 ⚠️）


def perform_switch(target_id: str, steps: SwitchSteps, *,
                   drain_seconds: float = SWITCH_DRAIN_TIMEOUT_S,
                   before_winddown: Optional[Callable[[], None]] = None,
                   is_settled: Optional[Callable[[], bool]] = None) -> str:
    """切换编排纯序列。返回 "ok" / "rejected-invalid" / "rejected-confirm"。

    铁律：坏包拒绝时**不写 settings**（当前角色不受影响）；写 settings 成功
    后才允许重启——写盘异常则中止且不重启，激活角色保持不变。
    before_winddown 在确认之后、收尾等待之前回调（气泡「稍等，把这句记完…」）。
    is_settled 透传 winddown（M+1 落评估完成信号后提前收工；缺省等满窗口）。
    """
    if steps.is_valid(target_id) is not True:
        logger.warning("切换被拒绝：目标包校验失败 %r（settings 未改动）", target_id)
        if steps.on_reject is not None:
            steps.on_reject(target_id)
        return "rejected-invalid"
    if not steps.confirm():
        logger.info("切换已取消（用户未确认）：%r", target_id)
        return "rejected-confirm"
    if before_winddown is not None:
        before_winddown()
    steps.interrupt()
    winddown(drain_seconds, is_settled=is_settled)   # 等后台评估落库（≤3s 强制）
    steps.write_settings(target_id)
    steps.restart()
    logger.info("切换完成：%r（重启后生效）", target_id)
    return "ok"


def _frozen_restart_helper(bat: Path, root: Path) -> list[str]:
    """冻结（exe）形态的重启助手：生成一次性 cmd 脚本（等 server.pid 消失或
    ~30s 超时 → 宽限 → 重拉启动 bat → 自删）。

    sys.executable 在 exe 里是 miyako.exe 自身而非 python（-c 会被 bootloader
    忽略），无法沿用源码形态的 `python -c` 助手；cmd 在 Windows 恒可用。
    CRLF 硬性（LF-only 脚本被 cmd 按读块劈碎——同 bat 坑）。路径一律绝对化：
    助手由 detached spawn 拉起，cwd 不受调用方控制。"""
    root = Path(root).resolve()
    bat = Path(bat).resolve()
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    helper = logs_dir / "_miyako_restart_helper.cmd"
    script = (
        "@echo off\r\n"
        "setlocal\r\n"
        f"set \"ROOT={root}\"\r\n"
        f"set \"BAT={bat}\"\r\n"
        "for /L %%i in (1,1,30) do (\r\n"
        "    if exist \"%ROOT%\\data\\server.pid\" ping -n 2 127.0.0.1 >nul\r\n"
        ")\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"       # client 退出宽限（quit_all 在 server kill 之后）
        "start \"\" \"%BAT%\"\r\n"
        "del \"%~f0\"\r\n"
    )
    helper.write_bytes(script.encode("utf-8"))
    return [str(helper)]


def schedule_restart(bat_path: Path | None = None,
                     *,
                     spawn: Optional[Callable[[list[str]], None]] = None) -> None:
    """派生 detached 助手：等旧端退出后重拉启动 bat（重启双端，「完全退出」复用）。

    顺序铁律：bat 拉起的新 client 会撞应用级互斥量——助手必须等旧端先死。
    助手以 server pid 文件消失 + 固定宽限为就绪信号（超时照常拉起：互斥量
    本身兜底防双开，最坏情形用户手动再点一次 bat）。
    形态分派：源码=pythonw -c 助手；冻结（exe）=一次性 cmd 助手（见
    _frozen_restart_helper——exe 的 sys.executable 不是 python）。
    """
    bat = bat_path or LAUNCHER_BAT
    root = Path(REPO_ROOT)
    if getattr(sys, "frozen", False):
        argv = _frozen_restart_helper(bat, root)
    else:
        code = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            f"root = Path({str(root)!r})\n"
            "deadline = time.time() + 10\n"
            "while time.time() < deadline and (root / 'data' / 'server.pid').exists():\n"
            "    time.sleep(0.2)\n"
            "time.sleep(1.0)\n"          # client 退出宽限（quit_all 在 server kill 之后）
            "os.startfile(str(Path(sys.argv[1])))\n"
        )
        python = sys.executable.replace("python.exe", "pythonw.exe")
        if not Path(python).is_file():
            python = sys.executable
        argv = [python, "-c", code, str(bat)]
    spawner = spawn or (lambda a: subprocess.Popen(
        a, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        close_fds=True))
    logger.info("已派生重启助手（重启双端）：%s", bat.name)
    spawner(argv)
