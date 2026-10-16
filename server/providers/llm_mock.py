"""Mock LLM（M1.7）：流式输出行式格式，覆盖全部 9 元情绪；零外部依赖可跑全链路。

few-shot 模板用于真实 provider（M2.2）组装 system prompt 时保持格式一致；
mock 本身直接产出符合 dialog_v1 的行。
"""
from __future__ import annotations

from typing import AsyncIterator

from server.contracts.base import build_contract
from server.providers.llm_base import LlmBase

# few-shot 模板：M2.2 真模型接入时的格式锚点（与 dialog_v1.format_prompt_reminder 同源）
FEWSHOT_PROMPT_TEMPLATE = """{format_reminder}

【示例（9 种情绪各一行）】
[e:normal]我在的，随时都可以聊。|いつでも話しかけてね。
[e:happy]今天也要加油哦！|今日も頑張ろうね！
[e:shy]突、突然说什么呢……|い、いきなり何言ってるの……
[e:pout]哼，才没有想你呢。|ふん、別に君のことなんて思ってないし。
[e:angry]这种事是不行的哦！|それはダメだと言ってるでしょ！
[e:sad]对不起……我有点难过。|ごめん……ちょっと悲しいの。
[e:surprise]诶？！真的假的？！|えっ？！ほんとに？！
[e:thinking]让我想想……嗯，这样如何？|えーっと……んー、こういうのはどう？
[e:sleepy]呼啊……再陪我一会儿……|ふぁあ……もうちょっとだけいて……

【用户】{{user_text}}
【你的回复】（每行一句，严格遵守上面的格式）"""

# 固定脚本：9 元情绪全覆盖，每行经 dialog_v1 可解析且 tts_ok=True
MOCK_SCRIPT_LINES = [
    "[e:normal]我在的，随时都可以聊。|いつでも話しかけてね。",
    "[e:happy]今天天气真好呢！|今日はいい天気だね！",
    "[e:shy]突、突然夸我什么的……|い、いきなり褒めるなんて……",
    "[e:pout]哼，才没有想你呢。|ふん、別に君のことなんて思ってないし。",
    "[e:angry]这种事是不行的哦！|それはダメだと言ってるでしょ！",
    "[e:sad]对不起……我有点难过。|ごめん……ちょっと悲しいの。",
    "[e:surprise]诶？！真的假的？！|えっ？！ほんとに？！",
    "[e:thinking]让我想想……嗯，这样如何？|えーっと……んー、こういうのはどう？",
    "[e:sleepy]呼啊……再陪我一会儿……|ふぁあ……もうちょっとだけいて……",
]


class MockLlm(LlmBase):
    name = "mock"
    few_shot_prompt = FEWSHOT_PROMPT_TEMPLATE.format(
        format_reminder=build_contract().format_prompt_reminder())

    async def stream_lines(self, user_text: str, *,
                           history: list[tuple[str, str]] | None = None,
                           system: str | None = None
                           ) -> AsyncIterator[str]:
        # 真实 LLM 流式输出会带换行；mock 保持一致，orchestrator 行缓冲按 \n 切分
        # system（M+1.6 逐轮覆盖缝）：mock 忽略（脚本输出固定，不依赖 prompt）
        for line in MOCK_SCRIPT_LINES:
            yield line + "\n"
