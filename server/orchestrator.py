"""orchestrator（M1.9）—— 句级流水线。

user_text → (mock 记忆占位) → llm 流式 → 行缓冲（行未完整缓存、换行触发，
完整行交契约——行缓冲收在本模块，**不建独立 sentence_parser.py**）→
dialog_v1.parse() → 每完整行：
  ①立即发 Sentence（闭集校验源 emotion_defs）
  ②文本过 voice_postprocess（text_zh / text_ja 双侧同步）
  ③create_task 异步送 TTS（合成源 = 处理后的 text_ja）
  ④TTS task 发 audio_begin(真实 sample_rate) → chunks → audio_end
全部完成后 ReplyEnd；Interrupt 取消 reply 主任务并级联取消全部 TTS 任务。
TTS 四级链 reply 级锁定（M2.4）：reply 开始 chain.lock() 探活锁定，句间不切换；
合成失败当场 chain.degrade() 降级重试本句，到末级 → Error{scope:"tts"}+纯气泡。
emotion 槽合并调用（M+1.3 实装）：reply_end 后后台任务一次调用 emotion 槽
出 {affection,trust,importance,open_question,reason}；解析失败先走借鉴⑤
修复重试（原输出 append+升级措辞+上限 2 次+轮间查取消）再静默缺省
（关系不变、importance=0.5、无钩子）；结果经持久化写队列（借鉴①）落库。
store/retriever/emotion_llm 未注入时保持占位行为（单测 hermetic）。
LLM 失败 fallback：tone_examples 随机一句 + Error{scope:"llm"}。
埋点：asr / memory / llm_first_line / voice_postprocess / tts_first_chunk（/metrics 出 p50/p95）。
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import random
import time
from collections import deque
from datetime import datetime
from typing import Callable, Optional

from server.contracts.base import build_contract
from server.emotion_defs import DEFAULT_EMOTION, SENTIMENT_CLOSE_SET
from server.memory.context import (HistoryCompressor, Section, compose,
                                   render_memory_notes)
from server.persona import Persona
from server.protocol import (
    AudioBegin,
    AudioChunk,
    AudioEnd,
    Error,
    Msg,
    RelState,
    ReplyBegin,
    ReplyEnd,
    Sentence,
    to_json,
)
from server.providers.factory import build_asr, build_llm, build_tts_chain
from server.providers.voice_postprocess import VoicePostprocess
from server.session_stats import SessionTokenStats
from server.tokens import estimate_tokens

logger = logging.getLogger(__name__)

_METRICS_MAXLEN = 2000   # 每埋点桶保留的最近样本数（环形，防常驻进程无界增长）

FALLBACK_TONE_EXAMPLES = [
    "[e:normal]我在的，随时都可以聊。|いつでも話しかけてね。",
    "[e:sad]抱歉……刚才走神了。|ごめん……ぼーっとしてた。",
]

# M+1.6 上下文预算（plan 定版比例；单位 = estimate_tokens，M2.6 同源）。
# 总额 4096：真实产线卡 persona.system_prompt 实测 ~1k token（含 18 条语气示例
# 与格式段），2048 会把 memory 段整段挤掉——实测定值，M+3.5 设置窗可调。
# user 槽为保留配额（用户文本不可砍）。
# v1.9.6 再实测：life 全量卡 persona 段实达 ~2463 tok（0.30×4096=1228 的 2 倍），
# 4096 会让 compose 把 memory 段整段砍掉（记忆召回失效）→ 容器升 8192。
CONTEXT_BUDGET_TOKENS = 8192
CONTEXT_BUDGET_RATIOS = {"persona": 0.30, "memory": 0.35, "history": 0.25, "user": 0.10}

# M+1.3：emotion 槽合并调用提示词（只输出 JSON）；修复重试用升级措辞指令
_EMOTION_EVAL_INSTRUCTION = (
    "你是桌面陪伴角色的后台情感评估器。阅读最新一轮对话，只输出一个 JSON 对象，"
    "禁止任何解释、前后缀或代码块围栏：\n"
    '{"affection": <-3到3的整数，好感变化>, "trust": <-3到3的整数，信任变化>, '
    '"importance": <0到1的小数，这轮对话的记忆重要度>, '
    '"open_question": <值得日后主动追问的话题，字符串或null>, '
    '"reason": "<不超过20字的理由>"}'
)
_EMOTION_REPAIR_INSTRUCTION = (
    "你上一条输出无法解析为规定的 JSON。请严格只输出一个 JSON 对象"
    "（以 { 开头、} 结尾，字段：affection/trust/importance/open_question/reason），"
    "不要输出任何其它文字。"
)


def _parse_emotion_json(text: str) -> Optional[dict]:
    """容错解析评估输出：截取首尾大括号间 JSON + 字段钳制；不可解析返回 None。"""
    if not text:
        return None
    s = text.strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(s[start:end + 1])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    def int_clamp(value) -> Optional[int]:
        try:
            return max(-3, min(3, int(round(float(value)))))
        except (TypeError, ValueError):
            return None

    try:
        importance = max(0.0, min(1.0, float(data.get("importance"))))
    except (TypeError, ValueError):
        return None
    affection, trust = int_clamp(data.get("affection")), int_clamp(data.get("trust"))
    if affection is None or trust is None:
        return None
    question = data.get("open_question")
    question = str(question).strip() if question is not None else ""
    return {"affection": affection, "trust": trust, "importance": importance,
            "open_question": question or None,
            "reason": str(data.get("reason") or "").strip()[:20]}


def percentile(values: list[float], pct: float) -> float:
    """最近邻排位百分位；空样本返回 0.0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


class Orchestrator:
    """单用户 orchestrator：一次只跑一个 reply；新输入到达时忽略并告警（Interrupt 优先）。

    character_card 读取经 server/persona.py 唯一入口（M2.1）：system prompt 送
    build_llm，tone_examples 只作 LLM 失败兜底（语义留本模块：随机一句 + Error）。
    """

    def __init__(self, *, settings: Optional[dict] = None, llm=None, tts=None,
                 tts_chain=None, asr=None, postprocess=None, contract=None,
                 tone_examples: Optional[list[str]] = None,
                 persona: Optional[Persona] = None,
                 notify: Optional[Callable[[str], None]] = None,
                 store=None, retriever=None, emotion_llm=None,
                 relationship=None, behavior=None,
                 on_reply_done: Optional[Callable[[], None]] = None) -> None:
        self.persona = persona or Persona()
        self.settings = settings or {}   # M2.5 /metrics 计费用（price 等读取源）
        self.reply_count = 0             # 完整 reply 计数（单次对话平均成本分母）
        # 会话级 token 统计（2026-09-17 用户需求）：本次使用（启动→关闭）的
        # 总量/单轮最高/最低 + 来源分桶（companion 陪玩独立视角）
        self.session_stats = SessionTokenStats()
        self.llm = llm or build_llm("chat", settings,
                                    system_prompt=self.persona.system_prompt)
        # M+1.3 记忆装配（main.py 注入真件；None=占位，单测不触真实库）
        self.store = store
        self.retriever = retriever
        self.emotion_llm = emotion_llm
        # M+2 关系与分寸（main.py 注入；None=占位）
        self.relationship = relationship
        self.behavior = behavior
        self.client_state: dict = {"foreground": None, "locked": None, "dnd": False}
        self._last_memory_context: list[dict] = []   # M+1.6 上下文预算消费
        self._notify = notify or (lambda msg: logger.warning("%s", msg))
        # TTS 装配优先级：注入单体（测试/旧路径）> 注入链 > settings 四级链（M2.4）。
        # 走链时 self.tts 初始为 None，reply 开始 lock() 探活锁定（句间不切换）。
        self._chain = tts_chain
        if tts is not None:
            self.tts = tts
        else:
            if self._chain is None:
                self._chain = build_tts_chain(settings, notify=self._notify)
            self.tts = None
        self.asr = asr or build_asr(settings)
        # 生产装配（main.py）不传 postprocess → 默认构建（读 .char 包 tts_config，
        # fail-open）；修复 M1.9 起的装配缺口：None 会在首句 _emit_sentence 崩溃
        self.postprocess = postprocess or VoicePostprocess()
        self.contract = contract or build_contract()
        self.tone_examples = (tone_examples
                              or list(self.persona.tone_examples)
                              or FALLBACK_TONE_EXAMPLES)
        # 埋点环形缓冲（maxlen）：常驻进程逐句累积无上限会拖住 /metrics 的
        # sorted()（现跑在事件循环线程上）——百分位只看最近样本语义不变
        self.metrics: dict[str, deque[float]] = {
            "asr": deque(maxlen=_METRICS_MAXLEN),
            "memory": deque(maxlen=_METRICS_MAXLEN),
            "llm_first_line": deque(maxlen=_METRICS_MAXLEN),
            "voice_postprocess": deque(maxlen=_METRICS_MAXLEN),
            "tts_first_chunk": deque(maxlen=_METRICS_MAXLEN),
        }
        self._dirty = 0
        self._total = 0
        self._tasks: set[asyncio.Task] = set()   # TTS 子任务集合（Interrupt 级联取消）
        self._bg_tasks: set[asyncio.Task] = set()  # 后台任务强引用（emotion 槽等，防中途回收）
        self._reply_task: Optional[asyncio.Task] = None
        self._send: Optional[Callable[[str], object]] = None
        self._send_lock = asyncio.Lock()         # 单发送队列（M2.6②）：全部下行串行化
        self._audio_buffer: list[str] = []       # base64 片段（AudioStart..AudioEnd）
        # seq 全序门链（M2.6②）：_audio_gate 恒指向「最近一句」的完成门，
        # 初始为常开——第 0 句无需等待；每 reply 开始时复位
        self._audio_gate = asyncio.Event()
        self._audio_gate.set()
        # M+1.6 上下文预算：Section 树组装（persona/memory 段）+ 阈值历史压缩
        self._history: list[tuple[str, str]] = []
        self._history_compressor = HistoryCompressor(
            budget_tokens=int(CONTEXT_BUDGET_RATIOS["history"] * CONTEXT_BUDGET_TOKENS),
            summarizer=self._summarize_history)
        self._llm_takes_system = "system" in inspect.signature(
            self.llm.stream_lines).parameters   # 旧测试替身自动降级静态 prompt
        # M-TOOL：reply 完成钩子（ReplyEnd 已发后、reply 任务体内同步调用——
        # 此刻 is_busy 仍为 True，消费方如需再次提交须自行延后，如 call_soon）
        self.on_reply_done = on_reply_done

    # ---------------- M+1.6 上下文组装 ----------------
    def _render_memory_section(self) -> str:
        """memory 槽：时间衰减标注 + 超槽位预算按三因子综合分从低砍尾巴。

        hits 已按综合分降序（retrieve 排序），砍尾即丢最低分行；行标注由
        render_memory_notes 统一（「三天前你说过…」）。
        """
        hits = list(self._last_memory_context or [])
        budget = int(CONTEXT_BUDGET_RATIOS["memory"] * CONTEXT_BUDGET_TOKENS)
        while hits:
            text = render_memory_notes(hits)
            if estimate_tokens(text) <= budget:
                return text
            hits = hits[:-1]
        return ""

    def _compose_system(self, include_memory: bool = True) -> str:
        """system prompt 逐轮组装：persona 段 + tone_hint 段（M+2.1 分支）+ memory 段。

        include_memory=False（M-TOOL 工具播报轮，域隔离定案）：只读
        persona+tone——播报不需要回忆，也杜绝记忆内容泄漏进播报词。
        预算恒按 persona+memory 全额计（v1.9.8 同源）：memory 段缺席≠腾出
        预算——若按 persona 单项比例（0.30×8192=2457）算，life 全量卡
        persona 实测 ~2463 tok 会吃满预算，compose 首段保底、次段起逐段
        丢弃 → tone_hint（关系语气）被静默挤掉。
        """
        sections = [
            Section(id="persona", priority=0.0,
                    render=lambda: self.persona.system_prompt),
            Section(id="tone_hint", priority=0.5, render=self._render_tone_hint),
        ]
        if include_memory:
            sections.append(Section(id="memory", priority=1.0,
                                    render=self._render_memory_section))
        total = int((CONTEXT_BUDGET_RATIOS["persona"]
                     + CONTEXT_BUDGET_RATIOS["memory"]) * CONTEXT_BUDGET_TOKENS)
        return compose(sections, budget_tokens=total)["text"]

    def _render_tone_hint(self) -> str:
        """关系分支 tone_hint（M+2.1）：当前 affection 首中分支的语气提示。"""
        if self.relationship is None:
            return ""
        try:
            hint = self.relationship.branch_tone_hint()
            return f"【当前关系阶段的语气提示】{hint}" if hint else ""
        except Exception:
            return ""

    async def _summarize_history(self, lines: list[str]) -> str:
        prompt = ("请把以下对话历史压缩成一段不超过 120 字的摘要，"
                  "保留事实与未决话题，不要评论：\n" + "\n".join(lines))
        return "".join([c async for c in self.llm.stream_lines(prompt)])

    async def _history_ready(self) -> list[tuple[str, str]]:
        try:
            return await self._history_compressor.compress(self._history)
        except Exception:
            logger.warning("历史压缩失败，退化为保最近 8 条", exc_info=True)
            return list(self._history[-8:])

    # ---------------- 连接接线 ----------------
    def attach(self, send: Callable[[str], object]) -> None:
        self._send = send

    def detach(self, send: Callable[[str], object]) -> None:
        # 绑定方法每次属性访问都产生新对象（websocket.send_text 两次取值 is 必不等）：
        # 曾致客户端断开后 _send 仍指向已关 socket，后续下行发射全部炸掉
        if self._send == send:
            self._send = None

    async def _emit(self, msg: Msg) -> None:
        if self._send is None:
            logger.debug("无下行连接，丢弃 %s", msg.type)
            return
        async with self._send_lock:              # 单发送队列：所有下行串行化
            await self._send(to_json(msg))

    async def _send_raw(self, raw: str) -> None:
        """已序列化的底层发送（须持 _send_lock；音频块整块发射用）。"""
        if self._send is None:
            logger.debug("无下行连接，丢弃原始帧")
            return
        await self._send(raw)

    # ---------------- 入口（WS 层调用） ----------------
    @property
    def is_busy(self) -> bool:
        """reply 进行中（M-CAM 观察器据此丢弃当帧）。"""
        return self._reply_task is not None and not self._reply_task.done()

    def submit_user_text(self, text: str) -> None:
        if self.is_busy:
            logger.warning("回复进行中，忽略新文本输入")
            return
        # reply 主任务只存 _reply_task（不进 _tasks：gather 不得等待自己）
        self._reply_task = asyncio.ensure_future(self.handle_user_text(text))

    def submit_behavior(self, text: str, source: str = "behavior") -> bool:
        """主动搭话入口（M-CAM 观察器等 trigger:"behavior" 源；忙碌则丢弃）。

        source：会话 token 统计来源桶（screen=识屏 / companion=陪玩 /
        behavior=其它主动信号），经 handle_user_text 透传。
        返回是否受理：忙碌丢弃返回 False——调用方（vision_observer 等）
        据此不记冷却/配额，配额只消耗在真正发起的搭话上。
        """
        if self.is_busy:
            logger.debug("reply 进行中，丢弃主动搭话")
            return False
        self._reply_task = asyncio.ensure_future(
            self.handle_user_text(text, trigger="behavior", source=source))
        return True

    def submit_tool(self, text: str) -> bool:
        """工具模式播报入口（M-TOOL；server/tool_announcer.py 消费）。

        trigger:"tool" 轮只读 persona——不检索记忆/不进历史/不写任何状态
        （域隔离定案，见 handle_user_text 各守卫点）。忙碌返回 False——
        调用方（ToolAnnouncer）据以入队，待 on_reply_done 钩子补播。
        """
        if self.is_busy:
            logger.debug("reply 进行中，工具播报交调用方排队")
            return False
        self._reply_task = asyncio.ensure_future(
            self.handle_user_text(text, trigger="tool", source="tool"))
        return True

    def on_audio_chunk(self, data_b64: str) -> None:
        self._audio_buffer.append(data_b64)

    def reset_audio_buffer(self) -> None:
        """AudioStart 语义：清空帧缓冲（防上一句残留帧污染下一句，M2.3 补线）。"""
        self._audio_buffer.clear()

    def on_audio_end(self, sample_rate: int = 16000) -> None:
        if self._reply_task is not None and not self._reply_task.done():
            logger.warning("回复进行中，忽略语音输入")
            self._audio_buffer.clear()
            return
        pcm = b"".join(base64.b64decode(part) for part in self._audio_buffer)
        self._audio_buffer.clear()
        self._reply_task = asyncio.ensure_future(self._audio_reply(pcm, sample_rate))

    def interrupt(self) -> None:
        """Interrupt：取消 reply 主任务（finally 级联取消全部 TTS 子任务）。"""
        if self._reply_task is not None and not self._reply_task.done():
            self._reply_task.cancel()
        for task in list(self._tasks):
            task.cancel()

    # ---------------- 句级流水线 ----------------
    async def handle_user_text(self, user_text: str, trigger: str = "user",
                               source: Optional[str] = None) -> None:
        """一条用户输入/主动信号的完整 reply 漏斗。

        source：会话级 token 统计的来源桶（user/behavior/screen/companion…，
        None=跟随 trigger）；轮次 token = llm_api 总量的轮首/轮末差值。
        M-TOOL：trigger=="tool"（工具播报轮）按域隔离定案走守卫式裁剪——
        不检索记忆、不带历史、memory 段省略、不进历史/对话落库、后台槽
        （emotion/关系/写回）全跳；AI Native 铁律对工具轮豁免（裁定，
        test_native_loop 只断言 user 轮）。
        """
        is_tool = trigger == "tool"    # M-TOOL 守卫开关（各步骤按此裁剪）
        await self._emit(ReplyBegin(trigger=trigger))
        from server.providers import llm_api as _llm_api
        usage_base = _llm_api.total_tokens()
        # 提前初始化：前段（探活锁定/记忆检索）失败走 fallback 句时也要能记账
        seq = 0
        reply_lines: list[str] = []                     # M+1.3 评估输入：本轮角色台词
        dialogue_lines: list[str] = []                  # 对话原文落库用（处理后的显示文本）
        try:
            # seq 全序门链复位（M2.6②）必须在一切可失败的前段步骤**之前**：
            # 上一 reply 被打断时旧门可能未 set，若先走 lock()/记忆检索失败
            # 落 fallback 句，合成任务会挂在陈旧未 set 门上（永久卡死 is_busy）
            self._audio_gate = asyncio.Event()
            self._audio_gate.set()
            if self._chain is not None:
                # reply 级锁定（M2.4）：探活一次，句间不切换（§4.2 音色一致性）
                self.tts = await self._chain.lock()
            # 口癖 reply 级节流（M2.6④）：每 reply 至多一次，固定规则
            self.postprocess.begin_reply()
            t0 = time.perf_counter()
            if is_tool:
                # M-TOOL 域隔离：播报轮只读 persona——不检索记忆（省 200ms
                # 预算），memory 段省略（杜绝记忆内容泄漏进播报词）
                history = []
                system_text = self._compose_system(include_memory=False)
            else:
                await self._memory_lookup(user_text)        # M+1.2 三因子检索（未装配=占位空）
                self._record("memory", t0)

                history = await self._history_ready()       # M+1.6 阈值历史压缩
                system_text = self._compose_system()        # M+1.6 Section 树组装
            llm_kwargs: dict = {"history": history}
            if self._llm_takes_system and system_text:
                llm_kwargs["system"] = system_text
            try:
                buffer = ""
                first_line = True
                async for chunk in self.llm.stream_lines(user_text, **llm_kwargs):
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if not line.strip():
                            continue
                        if first_line:
                            self._record("llm_first_line", t0)
                            first_line = False
                        dirty, text_zh = await self._emit_sentence(line, seq)
                        dialogue_lines.append(text_zh)
                        reply_lines.append(line.strip())
                        seq += 1
                        self.count_line(dirty)
                if buffer.strip():                          # 流末残行冲刷
                    if first_line:
                        self._record("llm_first_line", t0)
                        first_line = False
                    dirty, text_zh = await self._emit_sentence(buffer, seq)
                    dialogue_lines.append(text_zh)
                    reply_lines.append(buffer.strip())
                    seq += 1
                    self.count_line(dirty)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LLM 流式失败 → tone_examples fallback")
                await self._emit(Error(scope="llm", fallback="tone_examples 随机一句"))
                line = random.choice(self.tone_examples)
                dirty, text_zh = await self._emit_sentence(line, seq)
                dialogue_lines.append(text_zh)
                reply_lines.append(line)
                seq += 1
                self.count_line(dirty)

            if self._tasks:                                 # 等 TTS 子任务全部完成
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
        except asyncio.CancelledError:
            for task in list(self._tasks):                  # Interrupt 级联取消
                task.cancel()
            raise
        except Exception:
            # LLM 前段（探活锁定/记忆检索/历史压缩）异常：此前整个 reply 任务
            # 静默死亡——无 Error、无 ReplyEnd，客户端半双工静音门永不解除
            # （麦克风卡死到下一轮）。与 LLM 失败 fallback 同款兜底。
            logger.exception("回复管线前段失败 → Error + fallback 句")
            # 门链兜底复位：防御任何路径上残留的未 set 门（fallback 句的
            # 合成任务挂在旧门=本轮永不 ReplyEnd）
            self._audio_gate = asyncio.Event()
            self._audio_gate.set()
            try:
                await self._emit(Error(scope="llm", fallback="tone_examples 随机一句"))
                line = random.choice(self.tone_examples)
                dirty, text_zh = await self._emit_sentence(line, seq)
                dialogue_lines.append(text_zh)
                reply_lines.append(line)
                seq += 1
                self.count_line(dirty)
                if self._tasks:                             # 等 TTS 子任务全部完成
                    await asyncio.gather(*list(self._tasks), return_exceptions=True)
            except asyncio.CancelledError:
                for task in list(self._tasks):
                    task.cancel()
                raise
            except Exception:
                logger.exception("兜底句演出失败（本轮仍补 ReplyEnd）")

        await self._emit(ReplyEnd())
        self.reply_count += 1
        # 会话级 token 统计（2026-09-17）：轮末差值入桶（被 Interrupt 打断的
        # 轮经 CancelledError 上抛，不记录；后台 emotion 槽在轮末异步执行，
        # 其用量计入后续轮次——差值计量的已知取舍）
        try:
            self.session_stats.record_turn(
                _llm_api.total_tokens() - usage_base, source or trigger)
        except Exception:
            logger.debug("会话 token 统计记录失败（不影响 reply）", exc_info=True)
        # M+1.6 历史维护：本轮入账 + 阈值压缩（借鉴④；失败静默，不阻塞下行）。
        # M-TOOL：播报轮不进对话历史——防「工具痕迹」出现在后续闲聊上下文里。
        if not is_tool:
            try:
                self._history.append(("用户", user_text))
                if reply_lines:
                    self._history.append(("角色", reply_lines[0]))
                self._history = await self._history_compressor.compress(self._history)
            except Exception:
                logger.warning("历史维护失败（不影响本轮已完成的 reply）", exc_info=True)
        # 对话原文落库（查看历史数据源，M+3.5/四类件套补全）：单条 dialogue =
        # 一组「用户输入 + miyako 回复（处理后的显示文本）」；behavior/boot 轮
        # 用户侧只记触发标记；M-TOOL 播报轮完全不落库（域隔离定案）。
        # 走写队列（崩溃不丢）+ 滚动裁剪；检索池不召回 dialogue
        # （vector.RETRIEVAL_KINDS）。
        try:
            if dialogue_lines and self.store is not None and not is_tool:
                # 落库（sqlite 事务 + 裁剪）放线程池：store 有 threading.Lock
                # 串行化，但同步调用会占住事件循环（busy_timeout 竞争时最长 2s）
                await asyncio.to_thread(
                    self.store.record_dialogue,
                    user_text if trigger == "user" else None,
                    "\n".join(dialogue_lines), trigger=trigger)
        except Exception:
            logger.warning("对话原文落库失败（不影响本轮已完成的 reply）", exc_info=True)
        # M+1.3：reply_end 后状态写回（AI Native 铁律）——后台任务，不阻塞下行。
        # 保强引用（事件循环只持弱引用，多秒 LLM 调用可能被中途回收）。
        # M-TOOL：播报轮完全不写回（机械事件不得污染记忆/关系——域隔离定案）。
        if not is_tool:
            self._track_bg(asyncio.ensure_future(
                self._emotion_merge(user_text, "\n".join(reply_lines[:3]))))
        # M-TOOL：reply 完成钩子（ToolAnnouncer 补播队列在此 drain；失败不影响 reply）
        if self.on_reply_done is not None:
            try:
                self.on_reply_done()
            except Exception:
                logger.warning("on_reply_done 钩子失败（不影响 reply）", exc_info=True)

    async def _emit_sentence(self, raw_line: str, seq: int) -> tuple[bool, str]:
        """解析并演出一行；返回 (该行是否为脏行, 处理后的显示文本 text_zh)。"""
        t0 = time.perf_counter()
        parsed = self.contract.parse(raw_line)
        self._record("voice_postprocess", t0)  # 契约+后处理同段计时（含固定管线）
        emotion = parsed.emotion if parsed.emotion in SENTIMENT_CLOSE_SET else DEFAULT_EMOTION
        text_zh = self.postprocess.process(parsed.text_zh, "zh")
        text_ja = self.postprocess.process(parsed.text_ja, "ja")
        await self._emit(Sentence(seq=seq, text_zh=text_zh, text_ja=text_ja,
                                  emotion=emotion))
        # seq 全序门链（M2.6②）：新句先换门，脏行立即放行，有音频的交给合成任务
        prev_gate = self._audio_gate
        this_gate = asyncio.Event()
        self._audio_gate = this_gate
        if parsed.tts_ok:
            self._track(asyncio.ensure_future(
                self._synthesize(seq, text_ja, prev_gate, this_gate, emotion=emotion)))
            return False, text_zh
        this_gate.set()
        logger.warning("脏行（tts_ok=False）只走气泡：%.40r", raw_line)
        return True, text_zh

    async def _synthesize(self, seq: int, text_ja: str,
                          prev_gate: asyncio.Event, this_gate: asyncio.Event,
                          emotion: str | None = None) -> None:
        """合成一行，整块按 seq 全序发射（M2.6②：TTS 并发合成、发送串行化）。

        合成结果先缓冲；``prev_gate``（上一句完成门）放行后，本句
        AudioBegin→chunks→AudioEnd 在发送锁内**原子**发出——并发合成不产生
        跨 seq 交错。失败走既有降级链重试本句；finally 必放行 this_gate。
        """
        t0 = time.perf_counter()
        first = True
        try:
            while True:
                try:
                    # 绑定本句的合成实例：并发合成任务的 degrade() 会重绑
                    # self.tts——流后读 self.tts 会把别的句降级后的标称率
                    # 打到本句（跨任务串线变调）。每次降级重试重绑一次。
                    provider = self.tts
                    chunks: list[bytes] = []
                    async for chunk in provider.stream(text_ja, emotion=emotion):
                        if first:
                            self._record("tts_first_chunk", t0)
                            first = False
                        chunks.append(chunk)
                    # 流结束后从「产声实例」读采样率：真实值在流内解析 WAV
                    # 头时才落定（tts_base 契约「如实上报」；流前读到的恒是
                    # 标称值，cloud_clone 等厂商非标输出会整句变调）
                    rate = provider.sample_rate
                    await prev_gate.wait()   # 等前一句音频块发完（跨 seq 全序）
                    async with self._send_lock:
                        await self._send_raw(to_json(AudioBegin(seq=seq,
                                                                sample_rate=rate)))
                        for chunk in chunks:
                            await self._send_raw(to_json(AudioChunk(
                                seq=seq,
                                data=base64.b64encode(chunk).decode("ascii"))))
                        await self._send_raw(to_json(AudioEnd(seq=seq)))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 降级矩阵 TTS 行：中途失败当场降级重试本句（不再探活）；
                    # 已到末级（或无链）→ Error + 纯气泡。
                    nxt = self._chain.degrade() if self._chain is not None else None
                    if nxt is None:
                        logger.exception("TTS 合成失败且无更低级别（seq=%d）→ 纯气泡", seq)
                        await self._emit(Error(scope="tts",
                                               fallback="TTS 不可用，本句仅气泡显示"))
                        return
                    self.tts = nxt
                    logger.warning("TTS 合成失败（seq=%d）→ 当场降级 %s 重试本句",
                                   seq, nxt.name)
                    await self._emit(Error(scope="tts", fallback=f"TTS 降级 → {nxt.name}"))
        finally:
            this_gate.set()   # 取消/失败也放行下一句，门链永不死锁

    # ---------------- 记忆 / emotion 槽占位（M+1 实装） ----------------
    async def _memory_lookup(self, user_text: str) -> list[str]:
        """三因子记忆检索（M+1.2）；结果暂存供 M+1.6 上下文预算消费。"""
        if self.retriever is None:
            return []                        # 未装配：占位空 context
        hits = await self.retriever.retrieve(user_text, top_k=5)
        self._last_memory_context = hits
        return [h["content"] for h in hits]

    async def _emotion_merge(self, user_text: str = "", reply_text: str = "") -> dict:
        """emotion 槽合并调用（M+1.3，后台任务组）。

        一次调用出 {affection, trust, importance, open_question, reason}；
        解析失败先走借鉴⑤修复重试（原输出 append 历史 + 升级措辞指令 +
        全新消息列表不污染持久历史 + 上限 2 次 + 轮间 sleep(0) 查取消），
        仍失败静默缺省（关系不变、importance=0.5、无钩子；前台链 fail-open
        不重试）。结果经持久化写队列（借鉴①）落库：fact 条目带 importance、
        open_question 入 hooks、评估值存 emotion_tone 供 M+2.1 消费。
        """
        if self.store is None or self.emotion_llm is None:
            return {}                        # 未装配：占位行为
        dialog = f"最新一轮对话：\n用户：{user_text}\n角色：{reply_text}"
        result: Optional[dict] = None
        confidence = 0.5
        prompt = _EMOTION_EVAL_INSTRUCTION + "\n\n" + dialog
        history: Optional[list[tuple[str, str]]] = None
        try:
            for attempt in range(2):         # 借鉴⑤：上限 2 次（首试 + 1 次修复）
                output = "".join([chunk async for chunk in
                                  self.emotion_llm.stream_lines(prompt, history=history)])
                result = _parse_emotion_json(output)
                if result is not None:
                    confidence = 1.0 if attempt == 0 else 0.6   # 修复后置信度降档
                    break
                if attempt == 0:
                    # 修复重试：全新列表（deepcopy 语义），持久历史零污染
                    prompt = _EMOTION_REPAIR_INSTRUCTION
                    history = [("assistant", output)]
                await asyncio.sleep(0)       # 轮间查取消：cancel 在此落地
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("emotion 槽合并调用失败（静默）", exc_info=True)
            result = None
        if result is None:
            result = {"affection": 0, "trust": 0, "importance": 0.5,
                      "open_question": None, "reason": ""}
            confidence = 0.5
        try:
            now = datetime.now().isoformat(timespec="seconds")
            self.store.enqueue_write(kind="fact", content=user_text,
                                     importance=result["importance"],
                                     confidence=confidence, source="emotion_merge",
                                     created_at=now)
            if result["open_question"]:
                self.store.enqueue_write(kind="hook", content=result["open_question"],
                                         importance=result["importance"],
                                         confidence=confidence, source="emotion_merge",
                                         created_at=now)
            self.store.enqueue_write(
                kind="emotion_tone",
                content=json.dumps({"affection": result["affection"],
                                    "trust": result["trust"],
                                    "reason": result["reason"],
                                    "importance": result["importance"]},
                                   ensure_ascii=False),
                importance=result["importance"], confidence=confidence,
                source="emotion_merge", created_at=now)
            self.store.flush_pending()
        except Exception:
            logger.warning("评估结果入队失败（pending 已落盘，重启重放兜底）",
                           exc_info=True)
        # M+2.1：评估增量 → 关系维度写库 → rel_state 下行（失败静默，关系不变）
        await self._apply_relationship(result)
        return result

    async def _apply_relationship(self, result: dict) -> None:
        """评估增量落 dimensions 并推送 rel_state{affection,trust}（M+2.1）。"""
        if self.relationship is None:
            return
        try:
            new_values = self.relationship.apply_emotion(
                float(result.get("affection") or 0), float(result.get("trust") or 0))
            if new_values is not None:
                await self._emit(RelState(affection=new_values["affection"],
                                          trust=new_values["trust"]))
        except Exception:
            logger.warning("关系增量/rel_state 推送失败（静默，关系不变）",
                           exc_info=True)

    def update_client_state(self, *, foreground, locked, dnd) -> None:
        """客户端 state 上报消费（M+2.2）：仅内存态，不落库。"""
        self.client_state = {"foreground": foreground, "locked": locked, "dnd": dnd}
        if self.behavior is not None:
            try:
                self.behavior.on_state_changed(self.client_state)
            except Exception:
                logger.debug("behavior 状态回调失败", exc_info=True)

    # ---------------- 语音入口（ASR，M2.3 换真实现） ----------------
    async def _audio_reply(self, pcm16: bytes, sample_rate: int) -> None:
        t0 = time.perf_counter()
        try:
            text = await self.asr.transcribe(pcm16, sample_rate)
        except Exception:
            logger.exception("ASR 失败")
            await self._emit(Error(scope="asr", fallback="没听清"))
            return
        self._record("asr", t0)
        if not text.strip():
            # 🔑 幻听防线（v1.9.4）：ASR 质量闸过滤后的空转写 = 没有有效语音。
            # 静默丢弃——不回复、也不发「没听清」；谁都没说话时保持沉默才是不打扰。
            # 区别于降级矩阵「ASR 失败」行：那是真失败（异常），仍走没听清。
            logger.info("ASR 空转写（非语音/噪声被质量闸过滤）→ 静默丢弃")
            return
        await self.handle_user_text(text)

    # ---------------- 工具 ----------------
    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _track_bg(self, task: asyncio.Task) -> None:
        """后台任务（emotion 槽等）：保强引用防中途回收 + 异常落日志。
        独立于 _tasks（TTS 子任务集被 reply 末 gather，后台任务不得拖住下行）。"""
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("后台任务异常", exc_info=t.exception())

        task.add_done_callback(_done)

    def _record(self, name: str, t0: float) -> None:
        self.metrics[name].append((time.perf_counter() - t0) * 1000.0)

    def dirty_rate(self) -> float:
        return self._dirty / self._total if self._total else 0.0

    def count_line(self, dirty: bool) -> None:
        self._total += 1
        if dirty:
            self._dirty += 1
