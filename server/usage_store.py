"""token 用量跨重启累计（2026-09-12 Token 监控，用户需求③的持久化层）。

llm_api 的 per_role 注册表是内存态（server 重启归零）；本模块把每次计量增量
合并进 ``data/memory/{激活角色}.usage.json``——路径由 ``memory_db_path()``
派生（同名 .usage.json），因此「每角色一份」与冒烟测试的内存库重定向缝对它
自动生效。llm_api 只管计量、本模块只管持久化（原则 11）：经
``llm_api.set_usage_listener(accumulate)`` 挂接，写入走 tmp+os.replace 原子替换
+ 线程锁；损坏文件 fail-open 归零重建。/metrics 以 ``tokens_lifetime`` 段读出。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 可重入锁：accumulate（持锁）内部走 load_totals 的缓存读路径
_LOCK = threading.RLock()

_ENTRY_KEYS = ("prompt_tokens", "completion_tokens", "cached_tokens",
               "cache_reported_requests", "requests", "estimated_requests")


def usage_json_path(db_path: Optional[Path] = None) -> Path:
    """{激活角色}.db → 同名 .usage.json（测试可注入 db 路径）。"""
    from server.memory.store import memory_db_path

    real = Path(db_path) if db_path is not None else memory_db_path()
    return Path(str(real).removesuffix(".db") + ".usage.json")


_TOTALS_CACHE: dict[str, dict] = {}   # 路径 → 进程内权威累计（本进程唯一写方，见 load_totals）


def load_totals(path: Optional[Path] = None) -> dict:
    """读累计全量（损坏/缺失 → 空累计；fail-open 不抛）。

    进程内权威缓存：首次读盘入缓存，此后命中缓存（省每 LLM 请求一次
    read+JSON parse；写侧 write-through 不变，崩溃语义与旧实现一致）。
    前提=本进程是该文件的唯一写方（客户端只读不写）。返回深拷贝防调用方
    篡改缓存。"""
    real = usage_json_path(path)
    key = str(real)
    with _LOCK:
        cached = _TOTALS_CACHE.get(key)
        if cached is not None:
            # 文件被外部删除（手工清零重置）→ 缓存作废重建：一个 stat 的代价
            # 换「删文件即生效」，防下一次 accumulate 把已删文件复活
            if not real.is_file():
                _TOTALS_CACHE.pop(key, None)
            else:
                return copy.deepcopy(cached)
        try:
            if not real.is_file():
                data = {"roles": {}}
            else:
                data = json.loads(real.read_text(encoding="utf-8"))
                if not (isinstance(data, dict) and isinstance(data.get("roles"), dict)):
                    logger.warning("usage 累计文件形状非法，按空累计重建：%s", real)
                    data = {"roles": {}}
        except Exception:
            logger.warning("usage 累计文件读取失败，按空累计重建：%s", real, exc_info=True)
            data = {"roles": {}}
        _TOTALS_CACHE[key] = data
        return copy.deepcopy(data)


def accumulate(role: str, delta: dict, *, path: Optional[Path] = None) -> dict:
    """合并一次计量增量（llm_api 监听回调形态），返回合并后的全量。"""
    with _LOCK:
        totals = load_totals(path)
        entry = totals["roles"].setdefault(role, {key: 0 for key in _ENTRY_KEYS})
        for key, value in delta.items():
            if key in _ENTRY_KEYS:
                entry[key] = entry.get(key, 0) + max(0, int(value))
        totals["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        _write(totals, path)
        _TOTALS_CACHE[str(usage_json_path(path))] = totals
        return copy.deepcopy(totals)   # 深拷贝：返回值改动不得污染缓存权威副本


def _write(totals: dict, path: Optional[Path] = None) -> None:
    real = usage_json_path(path)
    real.parent.mkdir(parents=True, exist_ok=True)
    tmp = real.with_suffix(real.suffix + ".tmp")
    tmp.write_text(json.dumps(totals, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, real)
