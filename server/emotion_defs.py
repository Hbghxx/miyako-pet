"""9 元情感闭集 —— 全项目唯一事实源。

闭集校验（契约 parse、Sentence 模型校验）、提示词格式说明、演出层表情映射，
都只允许引用本模块的 SENTIMENT_CLOSE_SET，禁止任何文件出现第二份定义。
运行期不得修改该列表。
"""

SENTIMENT_CLOSE_SET = [
    "normal",
    "happy",
    "shy",
    "pout",
    "angry",
    "sad",
    "surprise",
    "thinking",
    "sleepy",
]

DEFAULT_EMOTION = "normal"
