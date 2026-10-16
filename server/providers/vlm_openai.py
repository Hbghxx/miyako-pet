"""OpenAI 兼容多模态 VLM provider（M-CAM/VIS.4）。

POST {base_url}/chat/completions，image_url 走 data URI base64；非流式。
配置：settings `vision:` 节（base_url/model/api_key_env），缺省回落 llm.api
对应值——用户一份 key 可同时喂对话与视觉。失败语义（降级矩阵，原则 3）：
任何失败 → VlmError 上抛，观察器静默丢弃（不影响对话主链路）。

安全：key 只从环境变量读（api_key_env 指定名）；base_url 仅允许 http/https；
凭据字面量禁止出现在源码/示例/测试。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from server.providers.llm_api import record_usage
from server.providers.vlm_base import VlmBase
from server.tokens import estimate_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)

_session_listener = None    # 会话级统计监听（main.py 装配；None=未挂）


def set_session_listener(fn) -> None:
    """挂会话统计监听（2026-09-17）：fn(total_tokens, source)，VLM 非轮次消耗
    经此计入本次使用的总量与来源桶（不计入轮次 max/min——轮=reply 漏斗）。"""
    global _session_listener
    _session_listener = fn


class VlmError(RuntimeError):
    """视觉理解失败——观察器静默丢弃当帧。"""


class VlmOpenai(VlmBase):
    name = "openai"

    def __init__(self, config: Optional[dict] = None, *,
                 http: Optional[httpx.AsyncClient] = None,
                 api_key: Optional[str] = None) -> None:
        cfg = config or {}
        vision = dict(cfg.get("vision") or {})
        llm_api = dict((cfg.get("llm") or {}).get("api") or {})
        self.base_url = str(vision.get("base_url")
                            or llm_api.get("base_url")
                            or os.getenv("OPENAI_BASE_URL") or "").rstrip("/")
        self.model = str(vision.get("model")
                         or llm_api.get("model")
                         or os.getenv("LLM_MODEL") or "")
        self.api_key_env = str(vision.get("api_key_env")
                               or llm_api.get("api_key_env") or "OPENAI_API_KEY")
        self._api_key = (api_key if api_key is not None
                         else os.getenv(self.api_key_env, ""))
        self.timeout_s = float(vision.get("timeout_s", 60.0))
        self._http = http  # 测试注入；生产懒建

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout_s)
        return self._http

    def _endpoint(self) -> str:
        if not self.base_url:
            raise VlmError("vision.base_url 未配置")
        if not (self.base_url.startswith("http://")
                or self.base_url.startswith("https://")):
            raise VlmError(f"base_url 仅允许 http/https：{self.base_url!r}")
        return f"{self.base_url}/chat/completions"

    async def describe(self, image_b64: str, text_hint: str | None = None,
                       source: str = "vision") -> str:
        """看图描述（M-CAM 观察与 M+3.3 识屏共用入口）。

        source：会话 token 统计来源桶（vision=摄像头/闲置识屏 /
        companion=陪玩画面理解）。
        """
        if not image_b64:
            raise VlmError("空图像")
        if not self.model:
            raise VlmError("vision.model 未配置")
        content: list[dict] = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
        if text_hint:
            content.append({"type": "text", "text": f"参考文本：{text_hint}"})
        payload = {"model": self.model, "stream": False,
                   "messages": [{"role": "user", "content": content}]}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            resp = await self._client().post(self._endpoint(),
                                             json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise VlmError(f"VLM 请求失败（{self._endpoint()}）：{exc}") from exc
        if resp.status_code != 200:
            raise VlmError(f"VLM HTTP {resp.status_code}：{resp.text[:200]}")
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise VlmError(f"VLM 响应结构异常：{exc}") from exc
        self._meter(data, payload, text, source)
        if not isinstance(text, str) or not text.strip():
            raise VlmError("VLM 返回空描述")
        return text.strip()

    def _meter(self, data: dict, payload: dict, text: str, source: str) -> None:
        """VLM 计量（2026-09-17 用户需求）：per_role vision 桶 + 会话统计监听。

        厂商报 usage 用真实值；否则文本估算（媒体占位清洗——图片 token 不入
        估算，偏保守偏低估）。计量失败不影响描述结果。
        """
        try:
            usage = data.get("usage") or {}
            prompt_tok = max(0, int(usage.get("prompt_tokens") or 0))
            completion_tok = max(0, int(usage.get("completion_tokens") or 0))
            estimated = not (prompt_tok or completion_tok)
            if estimated:
                prompt_tok = estimate_messages_tokens(payload["messages"])
                completion_tok = estimate_tokens(text)
            record_usage("vision", prompt_tok, completion_tok,
                         estimated=estimated)
            if _session_listener is not None:
                _session_listener(prompt_tok + completion_tok, source)
        except Exception:
            logger.debug("VLM 计量失败（不影响描述结果）", exc_info=True)
