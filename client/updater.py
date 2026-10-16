"""应用内检查更新（M+3.5.3）—— 托盘触发 → GET UPDATE_URL 比对 VERSION。

设计（plan 定案 + v1.9.2 透明度声明）：
  - 本产品**唯一外发面**：GET settings ``update.url``，请求只携带 URL 本身，
    不含任何用户数据（无 UA 指纹、无遥测、无用户内容）；响应预期为纯文本
    VERSION（如 ``0.7.0``）。
  - 版本比较为纯函数（分段数值比较，可容忍 ``v`` 前缀与 ``-dev`` 后缀）；
    远端较新 → 有更新；相同/较旧 → 已是最新；任何网络/解析失败 → error
    （托盘提示后静默，不阻塞任何功能，见降级矩阵）。
  - url 未配置（空/缺失）→ ``"unconfigured"``：托盘提示发布者未配置，非错误。

同步 httpx（托盘 QThread 内调用）；transport 注入缝供测试（MockTransport）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx

from server.constants import APP_ROOT

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0
VERSION_PATH = APP_ROOT / "VERSION"


def local_version(version_path: Path | None = None) -> str:
    """本地版本号（VERSION 单一来源；缺失 → "unknown"）。"""
    path = version_path or VERSION_PATH
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _numeric_part(part: str) -> int:
    """'7'/'0'/'dev0' 中的前导数字段；无数值段计 0（'-dev' 后缀已先行剥离）。"""
    digits = ""
    for ch in part:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else 0


def compare_versions(local: str, remote: str) -> int:
    """语义化分段比较：远端更新 → 1，相同 → 0，远端不更新 → -1。

    容忍 ``v`` 前缀与 ``-dev``/``-alpha`` 等后缀（后缀段参与比较但
    ``0.7.0-dev`` < ``0.7.0``：无后缀侧在该段视为更大）。
    """
    def parse(text: str) -> tuple[list[int], bool]:
        text = text.strip().lower().lstrip("v")
        suffix = ""
        if "-" in text:
            text, suffix = text.split("-", 1)
        parts = [_numeric_part(p) for p in text.split(".") if p != ""]
        while parts and parts[-1] == 0:
            parts.pop()                      # 0.7.0 与 0.7 等价
        return parts, (suffix == "")

    local_parts, local_clean = parse(local)
    remote_parts, remote_clean = parse(remote)
    length = max(len(local_parts), len(remote_parts))
    local_parts += [0] * (length - len(local_parts))
    remote_parts += [0] * (length - len(remote_parts))
    if local_parts == remote_parts:
        # 数值段相同：正式版 > 开发版（同版本号的 -dev 远端不算新版本）
        if remote_clean and not local_clean:
            return 1
        if local_clean and not remote_clean:
            return -1
        return 0
    return 1 if remote_parts > local_parts else -1


def check_for_update(url: str, *, local: str | None = None,
                     timeout: float = DEFAULT_TIMEOUT_S,
                     transport: Optional[httpx.BaseTransport] = None,
                     ) -> tuple[str, str]:
    """托盘入口。返回 (status, detail)：
    ("unconfigured", "") / ("update", 远端版本) / ("latest", 本地版本) /
    ("error", 失败摘要)。任何异常不外抛（降级矩阵：不阻塞任何功能）。
    """
    url = str(url or "").strip()
    if not url:
        return "unconfigured", ""
    current = local if local is not None else local_version()
    try:
        client_kwargs: dict = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url)
            resp.raise_for_status()
        remote = resp.text.strip()
    except Exception as exc:
        logger.info("检查更新失败（静默）：%s", exc.__class__.__name__)
        return "error", f"{exc.__class__.__name__}"
    if not remote:
        return "error", "EmptyResponse"
    relation = compare_versions(current, remote)
    if relation > 0:
        return "update", remote
    return "latest", current
