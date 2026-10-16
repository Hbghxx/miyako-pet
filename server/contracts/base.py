"""输出契约层接口 —— LLM 行式输出的解析与修复全部收在契约对象里（原则 2/11）。

职责边界：
  - ``parse`` 只处理「单行」，绝不持有跨行状态（行缓冲在 orchestrator，M1.9）；
  - 闭集校验只引用 server.emotion_defs（唯一事实源）；
  - 失败不抛异常：任何解析异常都降级为脏行结果 + warning（§4.1 dialog_v1 行）。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedLine:
    """契约对单行 LLM 输出的解析结果。

    tts_ok=False 表示本行不能送 TTS（如缺竖线的脏行），只能走纯气泡。
    """

    emotion: str
    text_zh: str
    text_ja: str
    tts_ok: bool


class OutputContract(ABC):
    """契约接口：prompt 侧给格式指令，输出侧做单行解析。"""

    name: str = "base"

    @abstractmethod
    def format_prompt_reminder(self) -> str:
        """返回追加到 system prompt 尾部的格式指令文本。"""

    @abstractmethod
    def parse(self, raw_line: str) -> ParsedLine:
        """解析单行；脏行按各自契约的降级语义处理，绝不抛异常。"""


def build_contract() -> OutputContract:
    """契约工厂：当前**硬绑定** dialog_v1（唯一实现，§4.2「Contract 单实现硬绑定」）。

    未来 ID 绑定预留位：若真的引入多契约（如 dialog_json_v1，见 §4.3 否决清单，
    翻案条件=经 Ollama 接入的弱模型教不会行式格式），在此按契约 ID 分发；
    现在刻意不写 dialog_json_v1、不做 settings 绑定段。
    """
    from server.contracts.dialog_v1 import DialogV1Contract  # 局部导入避免循环依赖

    return DialogV1Contract()
