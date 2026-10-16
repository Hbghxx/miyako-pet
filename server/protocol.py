"""消息协议唯一事实源（原则 2：全项目禁止在其它文件硬编码消息类型）。

线格式：JSON 对象，必含 ``type`` 与 ``proto_ver=1``；type 使用 snake_case，
与本模块 REGISTRY 的键一一对应。

双向复用：audio_chunk / audio_end / state 在两个方向使用同一模型——
上行（客户端→服务端）省略 ``seq``，下行（服务端→客户端）携带 ``seq``；
``state`` 上行携带 foreground/locked（M+2.2），下行携带 affection/trust（M+2.1）。

容错语义（plan M0.2）：未知 type / 非法 JSON / 校验失败 → warning + 返回 None，
绝不抛异常。下行不缓存不补发、无 seq ACK 的传输语义见 client/ws_client.py。
"""
from __future__ import annotations

import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError, field_validator

from server.emotion_defs import SENTIMENT_CLOSE_SET

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


class Msg(BaseModel):
    """所有消息的基类：type 判别字段 + 协议版本号。"""

    type: str
    proto_ver: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        """实例方法形态的序列化（与模块级 to_json 等价，调用侧更顺手）。"""
        return to_json(self)


# ---------------------------------------------------------------- 客户端 → 服务端

class UserText(Msg):
    """文本输入（输入框/契约兜底路径）。"""

    type: Literal["user_text"] = "user_text"
    text: str


class AudioStart(Msg):
    """开始一段语音采集。"""

    type: Literal["audio_start"] = "audio_start"


class AudioChunk(Msg):
    """音频块。上行省略 seq；下行（服务端→客户端 TTS 流）携带 seq。"""

    type: Literal["audio_chunk"] = "audio_chunk"
    seq: Optional[int] = None
    data: str  # base64 编码的 PCM16 块


class AudioEnd(Msg):
    """音频流结束。上行省略 seq；下行携带 seq。"""

    type: Literal["audio_end"] = "audio_end"
    seq: Optional[int] = None


class ScreenOcrLine(BaseModel):
    """识屏 OCR 单行结果（M+3.2：客户端本地 RapidOCR，纯文本上行）。"""

    text: str
    conf: float


class ScreenShot(Msg):
    """识屏截图。采集侧保证 base64 JPEG 且短边 ≤1024px（M+3 采集层职责）。

    hash：160px 灰度均值 hash 的 hex（M+3.1 采集侧算好；服务端 ① 去重闸比对，
    缺省=跳过该闸）。ocr：本地 OCR 结果；None = 未跑/不可用 → 服务端直接走
    VLM 路（§4.1「OCR 缺失/崩溃」行的手动热键降级）。文本路由的数据基础，
    规则匹配与「文本充分」判定都在服务端（M+3.3/M+3.4）。
    """

    type: Literal["screenshot"] = "screenshot"
    image: str  # base64 JPEG
    source: Literal["hotkey", "idle", "companion"]  # companion=陪玩场景帧（M-PLAY）
    hash: Optional[str] = None
    ocr: Optional[list[ScreenOcrLine]] = None


class CameraFrame(Msg):
    """摄像头感知帧（M-CAM/VIS.3）：base64 JPEG，≤camera.max_side。

    仅在 camera.enabled 且非 DND 时由客户端发出；服务端观察结果内存态
    用完即弃，帧与观察不落盘（隐私硬约束）。
    """

    type: Literal["camera_frame"] = "camera_frame"
    image: str  # base64 JPEG


ToolKind = Literal[
    "pomodoro_work_end",
    "pomodoro_break_end",
    "alarm",
    "countdown",
    "water",
    "sedentary",
    "hourly",
    "festival",
]
"""工具模式事件闭集（M-TOOL）：客户端定时器域的唯一 kind 词汇表。"""


class ToolEvent(Msg):
    """工具模式事件上行（M-TOOL）：客户端定时器到点，请求人格化播报。

    detail 是人读补充（如 "07:30" / "25"），服务端按 kind 选触发上下文模板。
    tool 轮走 reply 漏斗但**只读 persona**（不检索记忆/不进历史/不写任何
    状态——域隔离定案，plan M-TOOL）；服务端占线时入队（cap 2）reply 后补播。
    """

    type: Literal["tool_event"] = "tool_event"
    kind: ToolKind
    detail: str = ""


class Interrupt(Msg):
    """打断：barge-in 或用户主动停止，服务端须取消全部挂起任务。"""

    type: Literal["interrupt"] = "interrupt"


class ForegroundInfo(BaseModel):
    """前台窗口信息（客户端经 platform 层采集，服务端零系统调用）。"""

    title: str
    process: str


class State(Msg):
    """双向状态消息。

    上行（客户端→服务端）：foreground/locked/dnd —— 平台状态上报，foreground 为
    null 表示获取失败（gate 侧 fail-closed，见 M+2.3）；dnd 为托盘勿扰开关
    （M+2.3 gate 第一闸，源头上报）。服务端仅存内存态（仅本地回环，不落库）。
    （M0 遗留的 affection/trust 占位字段已删：关系数值下行走 RelState。）
    """

    type: Literal["state"] = "state"
    foreground: Optional[ForegroundInfo] = None
    locked: Optional[bool] = None
    dnd: Optional[bool] = None


class RelState(Msg):
    """关系数值下行（M+2.1，v1.8 改名：勿复用 state）——dimensions 落库后推送。"""

    type: Literal["rel_state"] = "rel_state"
    affection: float
    trust: float


class Ping(Msg):
    """心跳（客户端每 10s 一次）。"""

    type: Literal["ping"] = "ping"


class Auth(Msg):
    """连接鉴权（M2.6①）：连接后**首条**必须为 auth(token)；5s 未过 → 服务端断开 4401。

    token 文件 data/server.token（服务端启动时生成/复用；客户端每次连接现读）。
    鉴权闸门期间零业务消息——即使合法业务消息先到也按未鉴权拒绝。
    """

    type: Literal["auth"] = "auth"
    token: str


# ---------------------------------------------------------------- 服务端 → 客户端

class ReplyBegin(Msg):
    """一次回复开始。trigger 标注驱动源（tool=工具模式播报轮，M-TOOL）。"""

    type: Literal["reply_begin"] = "reply_begin"
    trigger: Literal["user", "behavior", "boot", "tool"]


class Sentence(Msg):
    """句级演出消息：气泡显示 text_zh，TTS 合成源为 text_ja（§0.4 定案）。"""

    type: Literal["sentence"] = "sentence"
    seq: int
    text_zh: str
    text_ja: str
    emotion: str  # 9 元闭集（emotion_defs），非法值在模型层直接拒绝
    motion: Optional[str] = None

    @field_validator("emotion")
    @classmethod
    def _emotion_in_closed_set(cls, v: str) -> str:
        if v not in SENTIMENT_CLOSE_SET:
            raise ValueError(f"emotion 不在 9 元闭集内: {v!r}")
        return v


class AudioBegin(Msg):
    """TTS 音频流开始；sample_rate 必须如实上报实际采样率。"""

    type: Literal["audio_begin"] = "audio_begin"
    seq: int
    sample_rate: int


class ReplyEnd(Msg):
    """一次回复结束（其后必有状态写回——AI Native 铁律，M+1 CI 断言）。"""

    type: Literal["reply_end"] = "reply_end"


class Error(Msg):
    """降级通知。scope 标注故障环节；fallback 描述客户端应有的表现/去向。"""

    type: Literal["error"] = "error"
    scope: Literal["llm", "tts", "asr", "vlm"]
    fallback: str = ""


class Pong(Msg):
    """心跳应答。"""

    type: Literal["pong"] = "pong"


# ---------------------------------------------------------------- 注册表与编解码

REGISTRY: dict[str, type[Msg]] = {
    m.model_fields["type"].default: m
    for m in (
        # 客户端 → 服务端
        UserText,
        AudioStart,
        AudioChunk,
        AudioEnd,
        ScreenShot,
        CameraFrame,
        ToolEvent,
        Interrupt,
        State,
        Ping,
        Auth,
        # 服务端 → 客户端（audio_chunk/audio_end/state 与上行复用同一模型）
        ReplyBegin,
        Sentence,
        AudioBegin,
        ReplyEnd,
        Error,
        Pong,
        RelState,
    )
}


def to_json(msg: Msg) -> str:
    """序列化为线格式（None 字段省略；type/proto_ver 恒在）。"""
    return msg.model_dump_json(exclude_none=True)


def from_json(raw: str) -> Optional[Msg]:
    """解析一条线格式消息。

    未知 type / 非法 JSON / 校验失败 → warning + None（绝不抛异常）；
    proto_ver 不一致 → warning 放行（宽松解析，便于日后升级观察）。
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("非 JSON 消息被丢弃: %.60r", raw)
        return None
    if not isinstance(obj, dict):
        logger.warning("消息不是 JSON 对象，丢弃: %.60r", raw)
        return None
    mtype = obj.get("type")
    cls = REGISTRY.get(mtype) if isinstance(mtype, str) else None
    if cls is None:
        logger.warning("未知消息 type=%r，丢弃（proto_ver=%r）", mtype, obj.get("proto_ver"))
        return None
    try:
        msg = cls.model_validate(obj)
    except ValidationError:
        logger.warning("消息校验失败 type=%r，丢弃", mtype, exc_info=True)
        return None
    if msg.proto_ver != PROTOCOL_VERSION:
        logger.warning("协议版本不一致：收到 %r，期望 %r（宽松放行）", msg.proto_ver, PROTOCOL_VERSION)
    return msg
