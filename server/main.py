"""FastAPI 服务端（M0.4 + M1.9 接线）。

安全铁律：仅监听 127.0.0.1 —— host 从 settings 读取，非 127.0.0.1 时
强制覆盖 + warning（``resolve_bind_host``，单一执行点）。
/metrics：管线延迟埋点 p50/p95 + 脏行率（M1.9 记录，M1.12 验收）。
优雅关闭：进程退出（SIGTERM/SIGINT → uvicorn lifespan shutdown）时断开全部 WS。
消息分发：Ping→Pong；UserText/AudioChunk/AudioEnd→orchestrator；
Interrupt→orchestrator.interrupt()；未知 type 在 from_json 内丢弃。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from server.constants import BIND_HOST
from server.constants import APP_ROOT as REPO_ROOT
from server.orchestrator import Orchestrator, percentile
from server.protocol import (
    Auth,
    CameraFrame,
    Ping,
    Pong,
    ScreenShot,
    State,
    UserText,
    AudioChunk,
    AudioEnd,
    AudioStart,
    ToolEvent,
    Interrupt,
    from_json,
)
from server.providers import llm_api as llm_api_module
from server import usage_store

logger = logging.getLogger(__name__)

TOKEN_PATH = REPO_ROOT / "data" / "server.token"
WS_AUTH_REJECTED = 4401   # M2.6①：鉴权失败/超时专用关闭码

_CONNECTIONS: set[WebSocket] = set()
_BG_TASKS: set[asyncio.Task] = set()   # fire-and-forget 任务强引用（防中途回收）


def _track_bg(task: asyncio.Task) -> None:
    """事件循环对 Task 只持弱引用：多秒的 VLM 观察/识屏路由可能被中途回收，
    异常也会变成无人认领的 warning。挂全局集合保活，完成自清理 + 异常落日志。"""
    _BG_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _BG_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("异步帧处理任务异常", exc_info=t.exception())

    task.add_done_callback(_done)


def load_or_create_token(path: Path | None = None) -> str:
    """读 data/server.token；缺失/为空/不可读 → 生成 256bit hex 并落盘。

    token 跨重启复用（不随重启轮换）：客户端每次连接现读文件，保证升级/重启
    后旧客户端仍能通过鉴权。文件在 .gitignore 与发布排除清单内。
    """
    p = path or TOKEN_PATH
    try:
        if p.is_file():
            existing = p.read_text(encoding="ascii").strip()
            if existing:
                return existing
    except Exception:
        logger.warning("token 文件读取失败，重新生成", exc_info=True)
    token = secrets.token_hex(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token, encoding="ascii")
    except Exception:
        logger.warning("token 文件写入失败（客户端将无法鉴权，请检查 data/ 权限）",
                       exc_info=True)
    return token


def resolve_bind_host(host: str) -> str:
    """安全铁律唯一执行点：非 127.0.0.1 的配置值一律强制覆盖 + warning。"""
    if host != BIND_HOST:
        logger.warning("settings.server.host=%r 非法，强制覆盖为 %r（仅本地回环）",
                       host, BIND_HOST)
        return BIND_HOST
    return host


def create_app(settings: dict | None = None,
               orchestrator: Orchestrator | None = None, *,
               auth_timeout: float = 5.0,
               token: str | None = None,
               vision_observer=None,
               screen_router=None) -> FastAPI:
    """构建 FastAPI 应用；settings / orchestrator / 鉴权 / 观察器 / 识屏路由均可注入（测试用）。

    auth_timeout：鉴权闸门等待秒数（生产 5s；测试调小加速）。
    """
    settings = settings or {}
    if orchestrator is not None:
        orch = orchestrator
    else:
        # M+1.3/M+2 装配：每角色一库（M-CHA.3 装配点按激活角色解析）+ 三因子检索
        # + emotion 槽合并调用 + 关系维度与主动搭话（M+2.1/2.3）。
        # 测试注入 orchestrator 时不触真实库（hermetic）。
        from server.behavior import Behavior
        from server.memory.store import MemoryStore
        from server.memory.vector import MemoryRetriever
        from server.providers.factory import build_llm
        from server.relationship import Relationship

        memory_store = MemoryStore()
        emotion_llm = build_llm("emotion", settings)
        orch = Orchestrator(settings=settings, store=memory_store,
                            retriever=MemoryRetriever(memory_store),
                            emotion_llm=emotion_llm)
        orch.relationship = Relationship(memory_store)
        orch.behavior = Behavior(store=memory_store, llm=emotion_llm,
                                 submit=orch.submit_behavior,
                                 state_getter=lambda: orch.client_state,
                                 settings=settings.get("behavior") or {})
    # M-TOOL 工具播报编排：注入 orchestrator 的测试同样挂上（hermetic，
    # 无事件时零行为）；播报轮语义在 orchestrator 的 trigger=="tool" 守卫内
    from server.tool_announcer import ToolAnnouncer

    tool_announcer = ToolAnnouncer(orch)
    ws_token = token if token is not None else load_or_create_token()
    if vision_observer is None:
        from server.vision_observer import VisionObserver
        from server.providers.factory import build_vlm

        vision_observer = VisionObserver(orchestrator=orch,
                                         vlm=build_vlm(settings),
                                         settings=settings)
    if screen_router is None:
        # M+3.3/M+3.4：识屏双路路由（规则引擎读 screen_triggers.rules；VLM 与
        # 摄像头观察共用同一 provider 形态）。注入 orchestrator 的测试若未带
        # behavior/vlm，路由各闸按 fail-closed/异常丢弃处理，不影响既有用例。
        from server.screen_router import ScreenRules, ScreenRouter

        screen_router = ScreenRouter(
            orchestrator=orch, vlm=getattr(vision_observer, "vlm", None),
            behavior=getattr(orch, "behavior", None),
            rules=ScreenRules(settings), settings=settings)
        # VLM 会话统计（2026-09-17）：画面理解消耗计入本次使用的 vlm_tokens
        # 与来源桶（vision=摄像头/闲置识屏，companion=陪玩画面理解）
        from server.providers import vlm_openai as vlm_openai_module

        vlm_openai_module.set_session_listener(
            lambda tokens, source: orch.session_stats.record_extra(tokens, source))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info("miyako server 启动（proto_ver=1，仅本地回环）")
        behavior_task = None
        if getattr(orch, "behavior", None) is not None:
            async def _behavior_loop():
                while True:
                    try:
                        await orch.behavior.tick()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug("behavior tick 异常（循环继续）", exc_info=True)
                    await asyncio.sleep(30)

            behavior_task = asyncio.ensure_future(_behavior_loop())
        yield
        if behavior_task is not None:
            behavior_task.cancel()
        # 优雅关闭：SIGTERM/SIGINT 都会走 lifespan shutdown，断开全部 WS
        for ws in list(_CONNECTIONS):
            try:
                await ws.close(code=1001)
            except Exception:
                logger.debug("关闭连接失败（可能已断开）", exc_info=True)
        _CONNECTIONS.clear()
        logger.info("miyako server 已关闭全部 WS 连接")

    app = FastAPI(lifespan=lifespan, title="miyako server")
    app.state.orchestrator = orch
    app.state.vision_observer = vision_observer
    app.state.screen_router = screen_router
    app.state.tool_announcer = tool_announcer
    # Token 跨重启累计（2026-09-12）：计量增量经监听落 data/memory/{角色}.usage.json
    llm_api_module.set_usage_listener(usage_store.accumulate)

    @app.get("/metrics")
    async def metrics() -> dict:
        # async：跑在事件循环线程上读 orch.metrics——此前同步路由经 threadpool
        # 并发遍历正在被 reply 任务 append 的列表（sorted 读到半扩容态的竞态）
        latency = {name: {"p50": percentile(values, 50),
                          "p95": percentile(values, 95),
                          "n": len(values)}
                   for name, values in orch.metrics.items()}
        # M2.5：分 role token 累计（llm_api 计量注册表）+ 单次对话平均成本
        # 2026-09-12 Token 监控：cached_tokens 单独计价（厂商缓存命中约 5 折）
        usage = llm_api_module.usage_snapshot()
        total_tokens = sum(e["prompt_tokens"] + e["completion_tokens"]
                           for e in usage.values())
        cached_tokens = sum(e.get("cached_tokens", 0) for e in usage.values())
        api_cfg = (orch.settings.get("llm") or {}).get("api") or {}
        price = api_cfg.get("price_cny_per_1m")
        price_cached = api_cfg.get("price_cny_per_1m_cached")
        if price:
            cached_price = price if price_cached is None else price_cached
            uncached = max(0, total_tokens - cached_tokens)
            total_cny = ((uncached * price + cached_tokens * cached_price)
                         / 1_000_000.0)
        else:
            total_cny = 0.0
        replies = orch.reply_count
        return {
            "proto_ver": 1,
            "latency": latency,
            "dirty_rate": orch.dirty_rate(),
            "tokens": {"per_role": usage,
                       "estimated": llm_api_module.usage_any_estimated()},
            # 跨重启累计（2026-09-12 Token 监控）：data/memory/{角色}.usage.json
            "tokens_lifetime": usage_store.load_totals(),
            # 会话级（本次使用：启动→关闭）token 统计（2026-09-17）：总量/
            # 单轮最高/最低 + 来源分桶（companion=陪玩独立视角）
            "tokens_session": orch.session_stats.snapshot(),
            "cost": {"price_cny_per_1m": price,
                     "price_cny_per_1m_cached": price_cached,
                     "cached_tokens": cached_tokens,
                     "total_tokens": total_tokens,
                     "replies": replies,
                     "total_cny": round(total_cny, 6),
                     "avg_per_reply_cny": round(total_cny / replies, 6) if replies else 0.0},
        }

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        # 鉴权闸门（M2.6①）：首条必须 auth(token)；超时/不符 → 4401。
        # 闸门期间零业务消息——先到的合法业务消息同样按未鉴权拒绝。
        try:
            first = await asyncio.wait_for(websocket.receive_text(),
                                           timeout=auth_timeout)
        except asyncio.TimeoutError:
            logger.warning("WS 鉴权超时（%.1fs 未收到 auth），断开 %d",
                           auth_timeout, WS_AUTH_REJECTED)
            await websocket.close(code=WS_AUTH_REJECTED)
            return
        except WebSocketDisconnect:
            return
        msg = from_json(first)
        if not isinstance(msg, Auth) or not hmac.compare_digest(msg.token, ws_token):
            logger.warning("WS 鉴权失败（token 缺失/不符），断开 %d", WS_AUTH_REJECTED)
            await websocket.close(code=WS_AUTH_REJECTED)
            return

        _CONNECTIONS.add(websocket)
        orch.attach(websocket.send_text)
        try:
            while True:
                raw = await websocket.receive_text()
                msg = from_json(raw)
                if isinstance(msg, Ping):
                    await websocket.send_text(Pong().to_json())
                elif isinstance(msg, UserText):
                    if getattr(orch, "behavior", None) is not None:
                        # 回扣判定+闲置计时（sqlite 读+写）：store 有锁串行化，
                        # 放线程池防 busy_timeout 竞争时堵住整个事件循环
                        await asyncio.to_thread(
                            orch.behavior.note_user_message, msg.text)
                    orch.submit_user_text(msg.text)
                elif isinstance(msg, State):
                    # M+2.2：仅内存态（仅本地回环，不落库）；foreground=null 由 gate fail-closed
                    orch.update_client_state(
                        foreground=msg.foreground.model_dump() if msg.foreground else None,
                        locked=msg.locked, dnd=bool(msg.dnd))
                elif isinstance(msg, AudioStart):
                    orch.reset_audio_buffer()   # 麦克风开：清残留帧（M2.3 补线）
                elif isinstance(msg, AudioChunk):
                    orch.on_audio_chunk(msg.data)
                elif isinstance(msg, AudioEnd):
                    orch.on_audio_end()
                elif isinstance(msg, CameraFrame):
                    # M-CAM：帧不落盘，观察结果内存态；异步提交不堵 WS 循环
                    _track_bg(asyncio.ensure_future(vision_observer.submit(msg.image)))
                elif isinstance(msg, ScreenShot):
                    # M+3.3：识屏双路路由——内容内存态用完即弃，异步不堵 WS 循环
                    if screen_router is not None:
                        _track_bg(asyncio.ensure_future(screen_router.handle(msg)))
                elif isinstance(msg, ToolEvent):
                    # M-TOOL：工具事件→播报编排（占线入队/闲时发起，同步不堵循环；
                    # reply 漏斗经 submit_tool 自行 ensure_future）
                    tool_announcer.submit(msg.kind, msg.detail)
                elif isinstance(msg, Interrupt):
                    orch.interrupt()
                # 其余类型当前不消费；未知 type 已在 from_json 内丢弃
        except WebSocketDisconnect:
            pass
        finally:
            orch.detach(websocket.send_text)
            _CONNECTIONS.discard(websocket)

    return app
