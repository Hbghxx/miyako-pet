"""WebSocket 客户端（M0.5）—— QThread 管理 asyncio WS 连接。

传输语义铁律（plan §0.4 / §4.2 / §5.4②）：
  - 断线时仅缓冲「上行」消息（无界队列），重连后严格按序补发；
  - 「下行」不缓冲、不补发：陪伴场景下静默丢弃优于错乱重放；
  - 协议不做 seq 级 ACK（禁止引入，见 §4.3；本文件不得出现任何补发下行逻辑）；
  - 连接被拒（ConnectionRefusedError，即时拒绝而非超时）**连续 3 次** →
    诊断信号（气泡提示安全模式）→ **停止重连**；
  - **握手超时（v1.9.4：TCP 连得上但对方不说 WebSocket = 端口被非 miyako
    程序占用的特征签名）连续 3 次** → 端口占用诊断 → 停止重连；
    两类计数互相独立，任何成功连接双双清零；
    其它网络类失败清零两者、维持无限指数退避（1s 起、30s 封顶）；
  - 心跳：连接期间每 10s 发送一次 Ping。

结构：``WsEngine`` 是无 Qt 依赖的可测内核（单测直接驱动）；
``WsClient`` 是 Qt 壳（QThread + Signal），把内核回调转成跨线程信号。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from server.constants import APP_ROOT
from server.protocol import Auth, Ping

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 10.0
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
REFUSED_LIMIT = 3
HANDSHAKE_TIMEOUT_LIMIT = 3   # 握手超时连续次数（v1.9.4：端口被占签名，见 run_forever）
OPEN_TIMEOUT_S = 5.0
WS_MAX_SIZE = 32 * 1024 * 1024
WS_AUTH_REJECTED = 4401   # 服务端鉴权失败专用关闭码（server/main.py 同源）

TOKEN_PATH = APP_ROOT / "data" / "server.token"

# 气泡诊断文案（§4.1「WS 连接被拒」行；文案改动需同步测试）。
# M-CHA.2：产品名硬编码改读激活角色展示名——函数化而非模块常量，防止 import 期读卡
def safe_mode_hint() -> str:
    """安全模式诊断文案（角色展示名数据化）。"""
    from server.persona import display_name   # 局部导入：引擎线程/单测不强制加载 persona

    return f"{display_name()} 服务未能启动，请双击安全模式查看原因"


def port_occupied_hint(url: str = "ws://127.0.0.1:9800/ws") -> str:
    """端口占用诊断文案（v1.9.4 真机验收定位：握手超时=有东西在监听但不是 miyako）。"""
    from urllib.parse import urlparse

    try:
        port = urlparse(url).port or 9800
    except ValueError:
        port = 9800
    return (f"{port} 端口好像被其它程序占用了，miyako 连不上去。"
            "请关闭占用该端口的程序后重新启动；也可在 config/settings.yaml "
            "修改 server.port 后重启。")


def default_token_provider() -> str:
    """每次连接现读 data/server.token（服务端重启轮换后自动跟上）；不可读返回空。"""
    try:
        return TOKEN_PATH.read_text(encoding="ascii").strip()
    except Exception:
        logger.warning("token 文件不可读，将发送空 auth（预期被服务端 4401 拒绝）")
        return ""


def next_backoff(current: float, cap: float = BACKOFF_CAP_S) -> float:
    """指数退避：×2 递增，cap 封顶（1→2→4→…→30→30…）。"""
    return min(current * 2.0, cap)


async def _default_connect_factory(url: str):
    """真实连接工厂；ConnectionRefusedError / asyncio.TimeoutError 由引擎区分处理。"""
    import websockets  # lazy import（原则 1）

    return await websockets.connect(url, open_timeout=OPEN_TIMEOUT_S, max_size=WS_MAX_SIZE)


class WsEngine:
    """可测内核：重连/心跳/上行缓冲/拒绝诊断，纯 asyncio，无 Qt 依赖。

    connect_factory 注入连接实现（单测用 FakeConn 脚本化断线/拒绝/超时）；
    on_* 回调在引擎的 asyncio 线程内被调用。
    """

    def __init__(
        self,
        url: str,
        *,
        connect_factory=None,
        auth_token_provider=None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        backoff_base: float = BACKOFF_BASE_S,
        backoff_cap: float = BACKOFF_CAP_S,
        refused_limit: int = REFUSED_LIMIT,
        timeout_limit: int = HANDSHAKE_TIMEOUT_LIMIT,
        on_message=None,
        on_connected=None,
        on_disconnected=None,
        on_diagnostic=None,
    ) -> None:
        self.url = url
        self._connect_factory = connect_factory or _default_connect_factory
        # M2.6①：每次连接现读 token（provider 可注入）
        self._auth_token_provider = auth_token_provider or default_token_provider
        self.heartbeat_interval = heartbeat_interval
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.refused_limit = refused_limit
        self.timeout_limit = timeout_limit
        self._on_message = on_message or (lambda raw: None)
        self._on_connected = on_connected or (lambda: None)
        self._on_disconnected = on_disconnected or (lambda: None)
        self._on_diagnostic = on_diagnostic or (lambda hint: None)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._conn = None
        self._connected = False
        self._stopping = False
        self._backoff = backoff_base
        self._refused_streak = 0
        self._timeout_streak = 0
        self._pending_uplink: deque[str] = deque()  # 上行缓冲：无界，仅 user_text/audio_* 等
        self._sleep_task: asyncio.Task | None = None
        self._flushing = False   # 重连补发进行中（见 _send_now 的顺序保证）

    # ---------------- 线程安全入口（Qt 主线程调用） ----------------

    def send(self, payload: str) -> None:
        """上行发送：已连接经 loop 直发；未连接进入无界缓冲，重连后按序补发。"""
        loop = self._loop
        if self._connected and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._send_now, payload)
        else:
            self._pending_uplink.append(payload)

    def stop(self) -> None:
        """请求停止：打断退避等待、关闭当前连接；run_forever 将自然返回。"""
        self._stopping = True
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._stop_now)

    # ---------------- loop 内部 ----------------

    def _stop_now(self) -> None:
        self._stopping = True
        if self._sleep_task is not None and not self._sleep_task.done():
            self._sleep_task.cancel()
        if self._conn is not None:
            asyncio.ensure_future(self._close(self._conn))

    @staticmethod
    async def _close(conn) -> None:
        try:
            await conn.close()
        except Exception:
            logger.debug("连接关闭失败（可能已断开）", exc_info=True)

    def _send_now(self, payload: str) -> None:
        if self._flushing:
            # 重连补发进行中：新上行强制排到缓冲尾——直接发送会越过缓冲里
            # 尚未补发的旧帧（AudioStart/Chunk/End 顺序敏感，契约=严格按序）
            self._pending_uplink.append(payload)
            return
        if self._connected and self._conn is not None:
            asyncio.ensure_future(self._safe_send(payload))
        else:
            self._pending_uplink.append(payload)

    async def _safe_send(self, payload: str) -> bool:
        try:
            await self._conn.send(payload)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("上行发送失败，视为断线", exc_info=True)
            return False

    async def run_forever(self) -> None:
        """主循环：连接 → 读循环 → 断线退避 → 重连。诊断命中或 stop 后返回。"""
        self._loop = asyncio.get_running_loop()
        while not self._stopping:
            try:
                conn = await self._connect_factory(self.url)
            except asyncio.CancelledError:
                raise
            except ConnectionRefusedError:
                # 即时拒绝（服务未启动），与超时类失败严格区分
                self._refused_streak += 1
                self._timeout_streak = 0
                logger.warning("连接被拒（连续 %d/%d）",
                               self._refused_streak, self.refused_limit)
                if self._refused_streak >= self.refused_limit:
                    self._on_diagnostic(safe_mode_hint())  # 气泡提示后停止重连
                    return
                await self._wait_backoff()
                continue
            except Exception as exc:
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    # 🔑 握手超时（v1.9.4 真机验收定位）：TCP 连得上但对方不说
                    # WebSocket——端口被非 miyako 程序占用的特征签名（miyako
                    # 慢启动只会 refused，端口未绑定；不会产生握手超时）。
                    # 因此不受慢启动豁免约束，连续 3 次即诊断并停止重连。
                    self._timeout_streak += 1
                    self._refused_streak = 0
                    logger.warning("WebSocket 握手超时（连续 %d/%d）——端口可能被占用",
                                   self._timeout_streak, self.timeout_limit)
                    if self._timeout_streak >= self.timeout_limit:
                        self._on_diagnostic(port_occupied_hint(self.url))
                        return
                    await self._wait_backoff()
                    continue
                # 其它网络类失败：打断两类连续计数，维持无限退避
                self._refused_streak = 0
                self._timeout_streak = 0
                logger.warning("连接失败（超时/网络类），维持退避重连", exc_info=True)
                await self._wait_backoff()
                continue

            self._refused_streak = 0
            self._timeout_streak = 0
            self._backoff = self.backoff_base
            self._conn = conn
            self._connected = True
            self._on_connected()
            heartbeat = asyncio.ensure_future(self._heartbeat_loop())
            try:
                await self._send_auth()          # M2.6①：首条 auth，先于一切上行补发
                await self._flush_pending()
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("连接中断，准备重连", exc_info=True)
            finally:
                heartbeat.cancel()
                self._connected = False
                self._conn = None
                self._on_disconnected()

            if not self._stopping:
                await self._wait_backoff()

    async def _send_auth(self) -> None:
        """连接后首条 auth(token)（M2.6①）；发送失败交给读循环/服务端超时收尾。"""
        token = self._auth_token_provider()
        await self._safe_send(Auth(token=token).to_json())

    async def _wait_backoff(self) -> None:
        delay = self._backoff
        self._backoff = next_backoff(self._backoff, self.backoff_cap)
        self._sleep_task = asyncio.ensure_future(asyncio.sleep(delay))
        try:
            await self._sleep_task
        except asyncio.CancelledError:
            pass  # stop() 打断等待；主循环检测 _stopping 后自然退出
        finally:
            self._sleep_task = None

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                await self._safe_send(Ping().to_json())
        except asyncio.CancelledError:
            raise

    async def _flush_pending(self) -> None:
        """重连补发：严格按入队顺序；中途失败把当前帧放回队首，交给重连逻辑。"""
        self._flushing = True
        try:
            while self._pending_uplink and self._connected:
                payload = self._pending_uplink.popleft()
                if not await self._safe_send(payload):
                    self._pending_uplink.appendleft(payload)
                    break
        finally:
            self._flushing = False

    async def _read_loop(self) -> None:
        # 下行语义（§5.4②）：收到即投递，绝不缓存、绝不补发、无 seq ACK。
        while True:
            try:
                raw = await self._conn.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # M2.6①：服务端 4401（token 不符/超时）→ 停止重连
                # （合法客户端不该发生；发生即 token 文件问题，重连无意义）
                code = getattr(exc, "code", None) or getattr(
                    getattr(exc, "rcvd", None), "code", None)
                if code == WS_AUTH_REJECTED:
                    logger.warning("服务端拒绝鉴权（%d）：停止重连", WS_AUTH_REJECTED)
                    self._stopping = True
                raise
            self._on_message(raw)


class WsClient(QThread):
    """Qt 壳：QThread 内跑 asyncio 引擎，回调转 Qt 信号（跨线程队列投递）。"""

    connected = Signal()
    disconnected = Signal()
    message_received = Signal(str)
    diagnostic = Signal(str)  # 命中拒绝诊断时发射，气泡展示 SAFE_MODE_HINT 后不再重连

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self._engine = WsEngine(
            url,
            on_message=self.message_received.emit,
            on_connected=self.connected.emit,
            on_disconnected=self.disconnected.emit,
            on_diagnostic=self.diagnostic.emit,
        )

    def run(self) -> None:  # QThread 入口
        try:
            asyncio.run(self._engine.run_forever())
        except asyncio.CancelledError:
            pass

    def send_text(self, payload: str) -> None:
        """线程安全上行（输入框/音频链路调用）。"""
        self._engine.send(payload)

    def stop(self) -> None:
        self._engine.stop()
