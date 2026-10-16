"""真实 LLM provider（M2.2）—— httpx 异步流式，OpenAI 兼容 /chat/completions。

计量（§0.4 token 决策）：请求带 ``stream_options:{"include_usage":true}``；厂商
明确报错不支持（HTTP 4xx 或流内 error 事件且报文提及 stream_options）→ 去参
重试一次，usage 改由 ``estimate_tokens()``（server/tokens.py，全项目唯一实现）
本地估算并记 ``estimated=true``——禁止计量静默归零；厂商接受参数但静默不给
usage 时同样走估算。分 role 计量累计进模块级注册表（M2.5 /metrics 消费）。

重试边界：报错发生在**产出增量之前**才允许去参重试（重试不重复输出）；流中途
失败直接上抛 LlmApiError → orchestrator 既有 fallback（tone_examples 随机 +
Error{scope:"llm"}，M1.9 路径不动）。

安全：API key 只从环境变量读取（settings 指定 env 名，缺省 OPENAI_API_KEY，
BYOK；本地 Ollama 无 key 时合法——不发送 Authorization 头）；base_url 仅允许
http/https。凭据字面量禁止出现在源码/示例/测试。
"""
from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator, Optional

import httpx

from server.providers.llm_base import LlmBase
from server.tokens import estimate_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 5.0


class LlmApiError(RuntimeError):
    """LLM 调用失败（配置/网络/HTTP/协议）——上抛给 orchestrator 走 fallback。"""


class _StreamOptionsUnsupported(RuntimeError):
    """厂商不支持 stream_options 的专有信号（去参重试的触发条件）。"""


# ---------------- 分 role 计量注册表（M2.5 /metrics 消费） ----------------
# 单进程单用户（原则 9），模块级注册表即全量事实；测试用 reset_usage() 复位。
_USAGE: dict[str, dict] = {}
_usage_listener = None    # 用量持久化监听（main.py 装配跨重启累计；None=未挂）


def set_usage_listener(fn) -> None:
    """挂/摘用量监听（形态 ``fn(role, delta_dict)``）；监听异常不反噬计量。"""
    global _usage_listener
    _usage_listener = fn


def cached_tokens_of(usage: dict) -> Optional[int]:
    """从厂商 usage 归一提取缓存命中 token（2026-09-12 Token 监控）。

    智谱/OpenAI 风格 ``prompt_tokens_details.cached_tokens`` 优先，DeepSeek
    风格 ``prompt_cache_hit_tokens`` 兜底；均未上报 → None（本地估算路径同样
    None——估算给不出缓存事实，宁可缺失不造假）。
    """
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        value = details.get("cached_tokens")
        if value:
            return max(0, int(value))
    value = usage.get("prompt_cache_hit_tokens")
    if value:
        return max(0, int(value))
    return None


def record_usage(role: str, prompt_tokens: int, completion_tokens: int,
                 *, estimated: bool, cached_tokens: Optional[int] = None) -> None:
    entry = _USAGE.setdefault(role, {
        "prompt_tokens": 0, "completion_tokens": 0,
        "cached_tokens": 0, "cache_reported_requests": 0,
        "requests": 0, "estimated_requests": 0,
    })
    entry["prompt_tokens"] += max(0, int(prompt_tokens))
    entry["completion_tokens"] += max(0, int(completion_tokens))
    entry["requests"] += 1
    if estimated:
        entry["estimated_requests"] += 1
    reported = cached_tokens is not None
    cached = max(0, int(cached_tokens)) if reported else 0
    if reported:
        entry["cached_tokens"] += cached
        entry["cache_reported_requests"] += 1
    if _usage_listener is not None:
        try:
            _usage_listener(role, {
                "prompt_tokens": max(0, int(prompt_tokens)),
                "completion_tokens": max(0, int(completion_tokens)),
                "cached_tokens": cached,
                "cache_reported_requests": 1 if reported else 0,
                "requests": 1,
                "estimated_requests": 1 if estimated else 0,
            })
        except Exception:
            logger.warning("usage 持久化监听异常（内存计量不受影响）", exc_info=True)


def usage_snapshot() -> dict:
    return {role: dict(entry) for role, entry in _USAGE.items()}


def total_tokens() -> int:
    """reply 轮次差值计量的基准：全部角色 prompt+completion 总 token。

    **排除 vision**——VLM 画面理解走 record_extra 直进会话统计（vlm_tokens），
    且其调用发生在轮外（screen_router/观察器），计入差值会把画面理解成本
    错算进下一轮的「单轮消耗」（v1.9.9 自查修正）。
    """
    return sum(e["prompt_tokens"] + e["completion_tokens"]
               for role, e in _USAGE.items() if role != "vision")


def usage_any_estimated() -> bool:
    return any(e["estimated_requests"] > 0 for e in _USAGE.values())


def reset_usage() -> None:
    """测试辅助：清空计量注册表。"""
    _USAGE.clear()


class LlmApi(LlmBase):
    name = "api"

    def __init__(self, config: Optional[dict] = None, *, role: str = "chat",
                 system_prompt: Optional[str] = None,
                 override: Optional[dict] = None,
                 http: Optional[httpx.AsyncClient] = None,
                 api_key: Optional[str] = None) -> None:
        llm_cfg = (config or {}).get("llm") or {}
        api = dict(llm_cfg.get("api") or {})
        for key, value in (override or {}).items():
            if key != "provider":       # 槽位 dict 覆盖（如 diary 点名大模型）
                api[key] = value
        self.role = role
        self.system_prompt = system_prompt or ""
        self.base_url = str(api.get("base_url")
                            or os.getenv("OPENAI_BASE_URL") or "").rstrip("/")
        self.model = str(api.get("model") or os.getenv("LLM_MODEL") or "")
        self.api_key_env = str(api.get("api_key_env") or "OPENAI_API_KEY")
        # 注入优先（测试）；生产从环境变量读取（凭据不进配置文件/源码）
        self._api_key = (api_key if api_key is not None
                         else os.getenv(self.api_key_env, ""))
        self.temperature = float(api.get("temperature", 0.8))
        self.timeout_s = float(api.get("timeout_s", 60.0))
        # 供应商私有参数透传（v1.9.3：如 thinking 等顶层键；浅合并——extra 顶层键
        # 覆盖同名基础键，谨慎使用）。槽位 dict 覆盖可携带同名键实现分槽差异化
        self.extra = dict(api.get("extra") or {})
        self._http = http  # 测试注入（httpx.MockTransport）；生产懒建

    # ---------------- 请求组装 ----------------
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s, connect=_CONNECT_TIMEOUT_S))
        return self._http

    def _messages(self, user_text: str,
                  history: Optional[list[tuple[str, str]]],
                  system: Optional[str] = None) -> list[dict]:
        messages: list[dict] = []
        effective_system = system if system is not None else self.system_prompt
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        for speaker, text in (history or []):
            role_name = "user" if str(speaker) in ("user", "用户") else "assistant"
            messages.append({"role": role_name, "content": str(text)})
        messages.append({"role": "user", "content": user_text})
        return messages

    def _endpoint(self) -> str:
        if not self.base_url:
            raise LlmApiError("llm.api.base_url 未配置（settings.yaml 或 OPENAI_BASE_URL）")
        if not (self.base_url.startswith("http://")
                or self.base_url.startswith("https://")):
            raise LlmApiError(f"base_url 仅允许 http/https：{self.base_url!r}")
        return f"{self.base_url}/chat/completions"

    # ---------------- 流式（LlmBase 契约不变；system=M+1.6 逐轮覆盖缝） ----------------
    async def stream_lines(self, user_text: str, *,
                           history: list[tuple[str, str]] | None = None,
                           system: str | None = None
                           ) -> AsyncIterator[str]:
        messages = self._messages(user_text, history, system)
        got_usage: Optional[dict] = None
        yielded: list[str] = []
        need_retry = False
        try:
            async for delta, usage in self._stream_once(messages, with_options=True):
                if delta:
                    yielded.append(delta)
                    yield delta
                if usage:
                    got_usage = usage
        except _StreamOptionsUnsupported as exc:
            if yielded:
                # 产出增量后才报错：重试会重复输出，直接走 fallback 路径
                raise LlmApiError("stream_options 报错发生在流中途，不重试") from exc
            logger.info("厂商不支持 stream_options → 去参重试 + 本地估算 usage")
            need_retry = True
        if need_retry:
            got_usage = None
            yielded = []
            async for delta, usage in self._stream_once(messages, with_options=False):
                if delta:
                    yielded.append(delta)
                    yield delta
                if usage:
                    got_usage = usage
        if got_usage:
            record_usage(self.role, int(got_usage.get("prompt_tokens") or 0),
                         int(got_usage.get("completion_tokens") or 0), estimated=False,
                         cached_tokens=cached_tokens_of(got_usage))
        else:
            # 禁止计量静默归零：去参重试或厂商静默省略 usage → 本地估算（estimated）
            # 按真实请求消息列表估算（含每轮 system 覆盖与信封开销）——按静态
            # 拼接会漏计 memory/tone 注入（与 vlm_openai._meter 同口径）
            record_usage(self.role, estimate_messages_tokens(messages),
                         estimate_tokens("".join(yielded)), estimated=True)

    async def _stream_once(self, messages: list[dict], *,
                           with_options: bool) -> AsyncIterator[tuple[str, Optional[dict]]]:
        payload: dict = {"model": self.model, "messages": messages,
                         "stream": True, "temperature": self.temperature}
        if self.extra:                       # 供应商私有参数透传（v1.9.3）
            payload.update(self.extra)
        if with_options:
            payload["stream_options"] = {"include_usage": True}
        headers: dict[str, str] = {}
        if self._api_key:               # 本地 Ollama 无 key：合法地不发送鉴权头
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with self._client().stream("POST", self._endpoint(),
                                             json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    self._raise_for_body(resp.status_code, body, with_options)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("LLM SSE 行非 JSON，跳过：%.80r", data)
                        continue
                    if obj.get("error"):
                        self._raise_for_body(200, json.dumps(
                            obj, ensure_ascii=False), with_options)
                    choices = obj.get("choices") or []
                    delta = ""
                    if choices:
                        delta = (choices[0].get("delta") or {}).get("content") or ""
                    usage = obj.get("usage")
                    yield delta, (usage if isinstance(usage, dict) and usage else None)
        except httpx.HTTPError as exc:
            raise LlmApiError(f"LLM 请求失败（{self._endpoint()}）：{exc}") from exc

    def _raise_for_body(self, status: int, body: str, with_options: bool) -> None:
        if with_options and "stream_options" in body:
            raise _StreamOptionsUnsupported(body[:200])
        raise LlmApiError(f"LLM HTTP {status}：{body[:200]}")
