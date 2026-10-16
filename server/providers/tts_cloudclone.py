"""云端音色克隆 TTS provider（用户 2026-09-12 需求②：显式指定档，不进自动链）。

**链位语义（与四级链①~④的关键差异）**：``providers.tts: cloud_clone`` 时链为
``[cloud_clone, mock]``——2026-09-12 用户定案：**音色一致性优先于声音存在性**，
听错误音色不如纯文本（edge/预录音频已整体移出链与 provider 列表），云克隆失败
即纯气泡 + 托盘 notify。

API 形态：**OpenAI 兼容 ``POST {base_url}/audio/speech``**（与 llm_api 同一款
「兼容端点 + BYOK」哲学）——阿里云百炼兼容模式（CosyVoice 克隆音色）、
SiliconFlow、OpenAI 等均暴露此形态；音色克隆的**录制/上传在服务商侧完成**
（各控制台上传 ~5s 干净录音得 voice id），miyako 只消费克隆 voice id。
请求 ``response_format=wav`` → 流式响应剥 WAV 头、按 fmt 段**如实解析采样率**
（tts_base 契约；不假设厂商输出率）后按块转发 PCM16。

安全（原则/README 透明度）：API key 只从环境变量读（``api_key_env`` 指定名，
缺省 CLOUD_TTS_API_KEY）；合成文本（text_ja）发往用户自己配置的服务商——
README「数据外发透明度」第【3】类，不配置 providers.tts: cloud_clone 即不发生。

失败语义（降级矩阵 TTS 行扩展）：配置缺失/无 key/网络失败/非 200/非 WAV/空
音频 → CloudCloneError → TtsChain 接管（探活失败锁定纯气泡；reply 中途失败
degrade() 同样落纯气泡）。探活=真实 dry-run（1 假名，结果缓存 5 分钟防每 reply
计费请求——与 ②edge 同语义；微文本成本可忽略）。
"""
from __future__ import annotations

import logging
import os
import struct
from typing import AsyncIterator, Optional

import httpx

from server.constants import load_settings
from server.providers.tts_base import TtsBase

logger = logging.getLogger(__name__)

API_KEY_ENV_DEFAULT = "CLOUD_TTS_API_KEY"
DRY_RUN_TEXT = "あ"           # 与 probe_edge 同款 1 假名探活
HTTP_TIMEOUT = httpx.Timeout(10.0, read=120.0)   # 云端首句合成读超时给足


class CloudCloneError(RuntimeError):
    """cloud_clone 档失败——TtsChain 接管（探活失败降 audiofile/中途 degrade）。"""


def _parse_wav_chunks(data: bytes) -> tuple[Optional[int], int, bytes]:
    """增量解析 WAV 流：返回 (sample_rate|None, 消耗字节数, 可转发 PCM)。

    RIFF fmt 段取真实采样率（支持 fmt 16/18/40 字节与 fmt 前的 LIST 等杂项块）；
    ``data`` 块之后的所有字节按 PCM16 直通。头未完整时返回 (None, 0, b"")。
    结构非法（非 RIFF/WAVE、缺 data 块判据）抛 CloudCloneError。
    """
    if len(data) < 12:
        return None, 0, b""
    if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise CloudCloneError(f"响应非 WAV（request response_format=wav）：{data[:8]!r}")
    offset = 12
    sample_rate: Optional[int] = None
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        (chunk_size,) = struct.unpack_from("<I", data, offset + 4)
        body_at = offset + 8
        if chunk_id == b"fmt ":
            if body_at + 16 > len(data):
                return None, 0, b""                      # fmt 未齐，继续攒
            (rate,) = struct.unpack_from("<I", data, body_at + 4)
            if rate <= 0:
                raise CloudCloneError(f"WAV fmt 采样率非法：{rate}")
            sample_rate = rate
        elif chunk_id == b"data":
            if sample_rate is None:
                raise CloudCloneError("WAV data 块先于 fmt 块（结构非法）")
            return sample_rate, body_at, data[body_at:]
        offset = body_at + chunk_size + (chunk_size & 1)   # 块按 2 字节对齐
    return None, 0, b""                                    # 头还没攒齐


class TtsCloudClone(TtsBase):
    name = "cloud_clone"
    sample_rate = 24000        # 标称值；实际以每流 WAV fmt 段解析为准（如实上报）

    def __init__(self, config: Optional[dict] = None, *,
                 http: Optional[httpx.AsyncClient] = None,
                 api_key: Optional[str] = None) -> None:
        if config is None:                       # 生产：唯一入口读 settings
            cfg = ((load_settings().get("providers") or {}).get("tts_cloudclone")
                   or {})
        else:                                    # 测试注入
            cfg = config
        self.base_url = str(cfg.get("base_url") or "").rstrip("/")
        self.model = str(cfg.get("model") or "")
        self.voice = str(cfg.get("voice") or "")
        self.api_key_env = str(cfg.get("api_key_env") or API_KEY_ENV_DEFAULT)
        self.response_format = str(cfg.get("response_format") or "wav")
        self.timeout_s = float(cfg.get("timeout_s", 60.0))
        self._api_key = (api_key if api_key is not None
                         else os.getenv(self.api_key_env, ""))
        self._http = http  # 测试注入；生产懒建

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s, connect=10.0, read=120.0))
        return self._http

    def _payload(self, text: str) -> dict:
        return {"model": self.model, "input": text, "voice": self.voice,
                "response_format": self.response_format}

    async def stream(self, text_ja: str, emotion: str | None = None) -> AsyncIterator[bytes]:
        """合成 text_ja（emotion 忽略——云端克隆 API 无情绪槽），按块 yield PCM16。"""
        if not self.base_url or not self.model or not self.voice:
            raise CloudCloneError(
                "cloud_clone 配置缺失（providers.tts_cloudclone 需 base_url/model/voice；"
                "音色在服务商控制台克隆后填 voice id）")
        if not self._api_key:
            raise CloudCloneError(
                f"云端 TTS key 未配置（环境变量 {self.api_key_env}，BYOK 不进 settings.yaml）")
        headers = {"Authorization": f"Bearer {self._api_key}"}
        got_audio = False
        header_done = False
        pending = b""
        try:
            async with self._client().stream(
                "POST", f"{self.base_url}/audio/speech",
                json=self._payload(text_ja), headers=headers) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread())[:200]
                    raise CloudCloneError(
                        f"云端 TTS HTTP {resp.status_code}：{body!r}"
                        "（key/余额/voice id 请在服务商控制台核对）")
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    if header_done:               # 头已剥：此后全部 PCM 直通
                        got_audio = True
                        yield chunk
                        continue
                    pending += chunk
                    rate, _consumed, pcm = _parse_wav_chunks(pending)
                    if rate is None:
                        continue                  # 头未攒齐，继续
                    self.sample_rate = rate       # tts_base 契约：如实上报
                    header_done = True
                    if pcm:
                        got_audio = True
                        yield pcm
                if not header_done:
                    raise CloudCloneError("云端 TTS 响应缺 WAV 头或为空")
        except CloudCloneError:
            raise
        except httpx.HTTPError as exc:
            raise CloudCloneError(
                f"云端 TTS 服务不可达（{self.base_url}，降级链将接管）：{exc}") from exc


async def probe_cloud_clone(timeout: float = 10.0, *,
                            tts: Optional[TtsCloudClone] = None) -> bool:
    """cloud_clone 探活：真实合成 1 假名 dry-run（计费微小，结果由链缓存 5 分钟）。

    ``tts`` 仅供测试注入（生产 None → 按唯一入口读 settings 构造）。
    """
    import asyncio

    try:
        instance = tts if tts is not None else TtsCloudClone()

        async def _first_pcm() -> bool:
            async for pcm in instance.stream(DRY_RUN_TEXT):
                if pcm:
                    return True
            return False

        return bool(await asyncio.wait_for(_first_pcm(), timeout))
    except Exception:
        return False
