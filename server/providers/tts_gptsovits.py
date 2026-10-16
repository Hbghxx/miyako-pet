"""GPT-SoVITS TTS provider（四级链①级，M2.4 前置实装，2026-09）。

架构（v1.9 §2.1/§4.4/§6.1）：GPT-SoVITS 是**外部本地 GPU 服务**——官方整合包
运行 api_v2.py 监听 127.0.0.1:9880，模型权重由服务侧持有（miyako 定制
GPT-SoVITS 仓 _setup/tts_infer_miyako.yaml：GPT_weights_v2/miyako-e15.ckpt +
SoVITS_weights_v2/miyako_e8_s3904.pth，启动入口「试听miyako.bat"）。本 provider
**只调 HTTP API，不加载模型**——进程边界与依赖都留在 GPT-SoVITS 侧。

请求（POST {api_url}/tts，api_v2 协议）：
  - text=text_ja / text_lang（§0.4：TTS 合成源 = text_ja）
  - ref_audio_path + prompt_text + prompt_lang：零沉参考音频（.char 包
    tts_config.yaml gptsovits 段；相对路径相对包根解析为绝对路径——api_v2
    读本地文件，须是服务进程可访问的路径）
  - media_type="wav" + streaming_mode：流式响应结构（api_v2 streaming_generator）
    为「首块 = wave 模块写的 44 字节 WAV 头（int16/mono/32k）+ 后续裸 PCM16 LE 块」。

本 provider 解析 WAV 头取**真实采样率**上报（tts_base 契约：sample_rate 如实），
剥头后按块转发 PCM16。streaming_mode 语义（api_v2 归一化）：0/1=非流式整段、
2=中质量流式、3=低延迟流式——默认 3（§2.4 首块 ≤500ms SLA 优先），可配置。

失败语义：参考音频缺失 / 连接失败 / 非 200（api_v2 出错返回 JSON 400）→ 抛
GptsovitsError，由 orchestrator 按降级矩阵 TTS 行接管（②级 edge 探活与 reply
级锁定链在 M2.4 补全；当前单 provider 选中态下失败即落既有 fallback 路径）。
"""
from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import yaml

from server.constants import resolve_character_package
from server.providers.tts_base import TtsBase

logger = logging.getLogger(__name__)

CHAR_DIR = resolve_character_package()
TTS_CONFIG_PATH = CHAR_DIR / "tts_config.yaml"

API_URL_DEFAULT = "http://127.0.0.1:9880"
WAV_HEADER_SIZE = 44          # wave 模块标准头（api_v2 wave_header_chunk 固定 44 字节）
# 首块后允许音频前静默等待合成；读超时给足（GPU 冷推理首句可达数秒）
HTTP_TIMEOUT = httpx.Timeout(10.0, read=120.0)


class GptsovitsError(RuntimeError):
    """①级失败——上抛给 orchestrator 走降级矩阵 TTS 行。"""


def _parse_wav_header(header: bytes) -> int:
    """从 44 字节标准 WAV 头解析采样率（RIFF fmt 段，little-endian）。

    api_v2 的头由 wave 模块生成（PCM int16/mono），结构固定：
    [0:4]RIFF [8:12]WAVE [12:16]fmt  [24:28]sample_rate（LE uint32）。
    """
    if len(header) < WAV_HEADER_SIZE or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise GptsovitsError(f"api_v2 流式响应首块非 WAV 头：{header[:8]!r}")
    (sample_rate,) = struct.unpack_from("<I", header, 24)
    if sample_rate <= 0:
        raise GptsovitsError(f"WAV 头采样率非法：{sample_rate}")
    return sample_rate


class GptsovitsTts(TtsBase):
    name = "gptsovits"
    sample_rate = 32000        # GPT-SoVITS v2 输出标称值；实际以每流 WAV 头解析为准

    def __init__(self, config_path: Path | None = None,
                 *, http: Optional[httpx.AsyncClient] = None,
                 package_dir: Path | None = None) -> None:
        cfg: dict = {}
        path = config_path or TTS_CONFIG_PATH
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            cfg = dict(loaded.get("gptsovits") or {})
        except Exception:
            logger.warning("tts_config.yaml gptsovits 段读取失败，用默认值", exc_info=True)

        self.api_url = str(cfg.get("api_url", API_URL_DEFAULT)).rstrip("/")
        self.text_lang = str(cfg.get("text_lang", "ja"))
        self.text_split_method = str(cfg.get("text_split_method", "cut5"))
        self.streaming_mode = int(cfg.get("streaming_mode", 3))
        self.prompt_text = str(cfg.get("prompt_text", ""))
        self.prompt_lang = str(cfg.get("prompt_lang", "ja"))

        ref = str(cfg.get("reference_audio") or "")
        pkg = package_dir or CHAR_DIR
        self.ref_audio_path = str((pkg / ref).resolve()) if ref else ""

        self._http = http  # 测试注入；生产懒建（见 _client）

        # life 卡候选池兜底（v1.9.6）：gptsovits 段缺 reference_audio/prompt_text
        # 时，从 reference_audio/candidates.yaml 取 rank#1 自动补位——V2 终审前的
        # 开箱即用形态（原型同款 rank#1=my0289）。仅补缺失键，不覆盖显式配置。
        if not self.ref_audio_path or not self.prompt_text:
            self._fill_from_candidates(pkg)

    def _fill_from_candidates(self, pkg_dir: Path) -> None:
        """life 卡候选池兜底（v1.9.6）：gptsovits 段缺参考配置时按 candidates.yaml
        补位。配对纪律：ref 与 prompt 必须同源（错配的转写会劣化合成）——
        缺 ref → 取 rank#1 连转写一起补；已有显式 ref → 只在同名单里找它的转写，
        找不到则保持缺失（请求期报错走降级链，好过张冠李戴）。"""
        meta = pkg_dir / "reference_audio" / "candidates.yaml"
        try:
            import yaml

            data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
            entries = [e for e in (data.get("candidates") or []) if isinstance(e, dict)]
            if not entries:
                return
            if not self.ref_audio_path:
                first = entries[0]                      # rank#1
                rel = str(first.get("file") or "")
                if rel:
                    self.ref_audio_path = str((pkg_dir / "reference_audio" / rel).resolve())
                if not self.prompt_text and first.get("text"):
                    self.prompt_text = str(first["text"])
                logger.info("gptsovits 段缺参考配置 → 候选池自动补位 rank#1（%s；V2 终审前的临时态）",
                            first.get("voice_id", "?"))
                return
            if not self.prompt_text:
                ref_name = Path(self.ref_audio_path).name
                match = next((e for e in entries
                              if Path(str(e.get("file") or "")).name == ref_name), None)
                if match and match.get("text"):
                    self.prompt_text = str(match["text"])
                    logger.info("gptsovits 段缺 prompt_text → 按参考音频文件名配对补位（%s）", ref_name)
        except Exception:
            logger.debug("候选池兜底读取失败", exc_info=True)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        return self._http

    async def _request_payload(self, text_ja: str) -> dict:
        if not self.ref_audio_path:
            raise GptsovitsError("gptsovits.reference_audio 未配置（.char 包 tts_config.yaml）")
        if not Path(self.ref_audio_path).is_file():
            raise GptsovitsError(f"参考音频缺失：{self.ref_audio_path}（preflight 角色包体检对应项）")
        if not self.prompt_text:
            raise GptsovitsError("gptsovits.prompt_text 未配置（参考音频转写，api_v2 合成必需）")
        return {
            "text": text_ja,
            "text_lang": self.text_lang,
            "ref_audio_path": self.ref_audio_path,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_lang,
            "text_split_method": self.text_split_method,
            "media_type": "wav",
            "streaming_mode": self.streaming_mode,
        }

    async def stream(self, text_ja: str, emotion: str | None = None) -> AsyncIterator[bytes]:
        """合成 text_ja，按块 yield PCM16 LE 字节（剥除流式 WAV 头）。"""
        payload = await self._request_payload(text_ja)
        try:
            async with self._client().stream(
                "POST", f"{self.api_url}/tts", json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread())[:200]
                    raise GptsovitsError(
                        f"api_v2 {resp.status_code}：{body!r}（服务在但合成失败，"
                        f"详见 GPT-SoVITS 控制台）")
                header_seen = False
                async for chunk in resp.aiter_bytes():
                    if not header_seen:
                        self.sample_rate = _parse_wav_header(chunk[:WAV_HEADER_SIZE])
                        chunk = chunk[WAV_HEADER_SIZE:]
                        header_seen = True
                    if chunk:
                        yield chunk
                if not header_seen:
                    raise GptsovitsError("api_v2 流式响应为空")
        except GptsovitsError:
            raise
        except httpx.HTTPError as exc:
            raise GptsovitsError(
                f"GPT-SoVITS 服务不可达（{self.api_url}；探活/启动见「试听miyako.bat」，"
                f"降级链将接管）：{exc}") from exc


async def probe_api_v2(api_url: str = API_URL_DEFAULT, timeout: float = 1.0) -> bool:
    """①级探活：TCP 连 api_v2 端口（与 preflight 同语义：端口在即通过）。

    模型冷启动期间端口已开但合成会失败——那种失败由 reply 中途 degrade 兜底，
    探活不做真实合成（GPU 服务经不起每 reply dry-run）。
    """
    import asyncio
    from urllib.parse import urlparse

    parsed = urlparse(api_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9880
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True
