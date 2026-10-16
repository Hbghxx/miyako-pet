"""provider 工厂（M1.7 / 原则 1）：读 settings 实例化，全部 lazy import。

路由规则（plan M2.2 / M-1.6 配置定版）：
  build_llm(role) 按 settings["llm"]["roles"][role] 取 provider 名；
  值为 "inherit" 或缺失 → 回落 settings["llm"]["provider"]。
未知 provider 名抛 ValueError——调用方（orchestrator）负责降级（Error{scope:...}）。

不支持任何 settings 级"平台/实现热切换"之外的动态行为；可插拔 =
改配置 + 重启，判定一次（原则 1 + §4.3）。
"""
from __future__ import annotations

import logging
from typing import Optional

from server.constants import load_settings as _load_settings_file

logger = logging.getLogger(__name__)


def load_settings(settings: Optional[dict] = None) -> dict:
    """测试注入 settings dict；生产经唯一入口读取（server.constants.load_settings）。"""
    if settings is not None:
        return settings
    return _load_settings_file()


def _llm_role_entry(llm_cfg: dict, role: str) -> tuple[str, dict]:
    """roles 槽位解析：字符串（"inherit"/provider 名）或 dict（点名真实 API + 覆盖）。"""
    entry = (llm_cfg.get("roles") or {}).get(role, "inherit")
    if isinstance(entry, dict):
        return str(entry.get("provider", "api")), {
            k: v for k, v in entry.items() if k != "provider"}
    return str(entry), {}


def _llm_provider_name(settings: dict, role: str) -> str:
    llm_cfg = settings.get("llm") or {}
    name, _ = _llm_role_entry(llm_cfg, role)
    if name == "inherit":
        name = str(llm_cfg.get("provider", "mock"))
    if name == "ollama":
        name = "api"    # OpenAI 兼容别名（Ollama /v1 端点，见 settings.example.yaml）
    return name


def build_llm(role: str = "chat", settings: Optional[dict] = None,
              system_prompt: Optional[str] = None):
    """按角色构建 LLM provider（chat/emotion/correction/diary 四槽位）。

    system_prompt（M2.1 persona 产物）只对真实 API provider 生效；mock 的
    few-shot 模板自带格式锚点，不消费该参数（llm_mock 模块注释）。
    """
    cfg = load_settings(settings)
    llm_cfg = cfg.get("llm") or {}
    name, override = _llm_role_entry(llm_cfg, role)
    if name == "inherit":
        name = str(llm_cfg.get("provider", "mock"))
    if name == "ollama":
        name = "api"    # OpenAI 兼容别名
    if name == "mock":
        from server.providers.llm_mock import MockLlm  # lazy

        return MockLlm()
    if name == "api":
        from server.providers.llm_api import LlmApi  # lazy

        return LlmApi(cfg, role=role, system_prompt=system_prompt,
                      override=override or None)
    raise ValueError(f"未知 LLM provider：{name!r}（role={role!r}）")


def build_tts(settings: Optional[dict] = None):
    cfg = load_settings(settings)
    name = str((cfg.get("providers") or {}).get("tts", "mock"))
    if name == "mock":
        from server.providers.tts_mock import MockTts  # lazy

        return MockTts()
    if name == "gptsovits":
        # 音色级（外部 api_v2 服务，探活 9880；配置在 .char 包 tts_config.yaml）
        from server.providers.tts_gptsovits import GptsovitsTts  # lazy

        return GptsovitsTts()
    if name == "cloud_clone":
        # 云端克隆音色（OpenAI 兼容 /audio/speech）；链式装配见 build_tts_chain
        from server.providers.tts_cloudclone import TtsCloudClone  # lazy

        return TtsCloudClone()
    raise ValueError(f"未知 TTS provider：{name!r}")


def build_tts_chain(settings: Optional[dict] = None, notify=None):
    """TTS 音色链（2026-09-12 用户定案收敛）：providers.tts 选**唯一音色级**，
    失败直接落 mock 纯气泡——**不再有中间音色级**。

    用户定案理由：「听错误音色不如纯文本」——音色一致性优先于声音存在性；
    edge-tts（机械音）与 audiofile（预录音频）整体移出链与 provider 列表。

      gptsovits   → [gptsovits(TCP 探活 9880), mock(静音=纯气泡)]
      cloud_clone → [cloud_clone(真实 dry-run 探活, 5min 缓存), mock(静音)]
      mock        → [mock(有声)]（零探活，默认/CI 全 mock 零联网）
      未知值      → [mock(有声)]（fail-open + warning）

    链尾 mock 双形态（2026-09-18 补收敛漏网）：mock 是**降级落点**（链上
    还有音色级）时 silent=True 纯气泡——「听错误音色不如纯文本」/release
    notes「未启动=纯文本」的兑现（此前降级落 mock 发 1kHz 正弦滴声）；
    mock 是**配置的 provider**（出厂默认/CI）时保持有声，驱动口型与音频链。
    """
    from server.providers.tts_chain import TtsChain, TtsLevel
    from server.providers.tts_mock import MockTts  # lazy

    cfg = load_settings(settings)
    name = str((cfg.get("providers") or {}).get("tts", "mock"))

    levels: list[TtsLevel] = []
    if name == "gptsovits":
        from server.providers.tts_gptsovits import (GptsovitsTts,
                                                    probe_api_v2)  # lazy

        probe_ref = GptsovitsTts()  # 仅读 tts_config 拿 api_url（不发请求）
        levels.append(TtsLevel("gptsovits", GptsovitsTts,
                               probe=lambda: probe_api_v2(probe_ref.api_url)))
    elif name == "cloud_clone":
        from server.providers.tts_cloudclone import (TtsCloudClone,
                                                     probe_cloud_clone)  # lazy

        levels.append(TtsLevel("cloud_clone", TtsCloudClone,
                               probe=probe_cloud_clone,
                               probe_cache_ttl=300.0))
    elif name != "mock":
        logger.warning("providers.tts=%r 不在支持列表（gptsovits/cloud_clone/mock）"
                       "→ 按 mock 处理（fail-open）", name)
    mock_silent = bool(levels)          # 链上有音色级 → mock 是降级落点 → 纯气泡
    levels.append(TtsLevel("mock", (lambda silent=mock_silent: MockTts(silent=silent))))

    return TtsChain(levels, notify=notify)


def build_asr(settings: Optional[dict] = None):
    cfg = load_settings(settings)
    name = str((cfg.get("providers") or {}).get("asr", "mock"))
    if name == "mock":
        from server.providers.asr_mock import MockAsr  # lazy

        return MockAsr()
    if name == "whisper":
        from server.providers.asr_whisper import AsrWhisper  # lazy（M2.3 实装）

        return AsrWhisper(cfg)
    raise ValueError(f"未知 ASR provider：{name!r}")


def build_vlm(settings: Optional[dict] = None):
    cfg = load_settings(settings)
    name = str((cfg.get("providers") or {}).get("vlm", "mock"))
    if name == "mock":
        from server.providers.vlm_mock import MockVlm  # lazy

        return MockVlm()
    if name == "openai":
        # OpenAI 兼容多模态（M-CAM 摄像头感知 / M+3.3 识屏）；key 从环境读
        from server.providers.vlm_openai import VlmOpenai  # lazy

        return VlmOpenai(cfg)
    raise ValueError(f"未知 VLM provider：{name!r}")
