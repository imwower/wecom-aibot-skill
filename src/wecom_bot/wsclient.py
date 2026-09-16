"""企业微信智能机器人长连接客户端。

职责：建连 → aibot_subscribe 鉴权 → 30 秒心跳 → 指数退避重连；
发送帧并按 req_id 等待回执（同一 req_id 串行，因为流式回复会复用回调的 req_id）。

注意：同一个 bot 同时只能有一条长连接，新连接会踢掉旧连接，所以本机必须
只跑一个 daemon（见 lockfile.py）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Deque, Dict, Optional

import websockets
from websockets.asyncio.client import ClientConnection

from . import protocol as P

log = logging.getLogger("wecom.ws")


class AckError(RuntimeError):
    """服务端回执 errcode != 0。"""

    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f"errcode={errcode} errmsg={errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


class NotConnected(RuntimeError):
    pass


class WeComWsClient:
    def __init__(
        self,
        bot_id: str,
        secret: str,
        *,
        ws_url: str = "wss://openws.work.weixin.qq.com",
        heartbeat_interval: float = 30.0,
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        ack_timeout: float = 5.0,
        on_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self._bot_id = bot_id
        self._secret = secret
        self._ws_url = ws_url
        self._heartbeat_interval = heartbeat_interval
        self._base_delay = reconnect_base_delay
        self._max_delay = reconnect_max_delay
        self._ack_timeout = ack_timeout
        self._on_callback = on_callback
        self._ssl = ssl_context

        self._ws: Optional[ClientConnection] = None
        self._run_task: Optional[asyncio.Task] = None
        self._hb_task: Optional[asyncio.Task] = None
        self._stopping = False

        self.connected = False
        self.authenticated = False
        self.conn_gen = 0            # 每次重连 +1，用于判断旧 req_id 是否还有效
        self.last_error: Optional[str] = None
        self.last_auth_at: Optional[float] = None
        self.reconnect_attempts = 0
        self.missed_pongs = 0

        # req_id -> 等待回执的 Future 队列（FIFO）
        self._waiters: Dict[str, Deque[asyncio.Future]] = defaultdict(deque)
        # req_id -> 串行发送锁
        self._send_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._authed_event = asyncio.Event()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._run_task is None or self._run_task.done():
            self._stopping = False
            self._run_task = asyncio.ensure_future(self._run_forever())

    async def stop(self) -> None:
        self._stopping = True
        self._cancel_heartbeat()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
        self._run_task = None
        self.connected = False
        self.authenticated = False
        self._authed_event.clear()

    async def wait_authenticated(self, timeout: float = 15.0) -> bool:
        try:
            await asyncio.wait_for(self._authed_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run_forever(self) -> None:
        """连接主循环：断开后按指数退避重连，永不放弃。"""
        while not self._stopping:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 建连失败
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("建连失败：%s", self.last_error)
            finally:
                self._on_disconnected()

            if self._stopping:
                break
            self.reconnect_attempts += 1
            delay = min(self._base_delay * (2 ** (self.reconnect_attempts - 1)), self._max_delay)
            log.info("%.1f 秒后重连（第 %d 次）", delay, self.reconnect_attempts)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    async def _connect_once(self) -> None:
        log.info("连接 %s", self._ws_url)
        kwargs: Dict[str, Any] = {"ping_interval": None, "ping_timeout": None, "close_timeout": 5}
        if self._ws_url.startswith("wss://"):
            kwargs["ssl"] = self._ssl or ssl.create_default_context()
        async with websockets.connect(self._ws_url, **kwargs) as ws:
            self._ws = ws
            self.connected = True
            self.conn_gen += 1
            self.missed_pongs = 0
            log.info("长连接已建立（第 %d 代），发送鉴权帧", self.conn_gen)
            await self._send_raw(P.subscribe_frame(self._bot_id, self._secret))
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("收到非 JSON 帧，已忽略")
                    continue
                await self._handle_frame(frame)

    def _on_disconnected(self) -> None:
        was = self.connected
        self._ws = None
        self.connected = False
        self.authenticated = False
        self._authed_event.clear()
        self._cancel_heartbeat()
        self._fail_all_waiters("长连接已断开")
        if was:
            log.warning("长连接断开")

    # ---------- 帧处理 ----------

    async def _handle_frame(self, frame: Dict[str, Any]) -> None:
        cmd = frame.get("cmd")
        if cmd in (P.CMD_MSG_CALLBACK, P.CMD_EVENT_CALLBACK):
            if self._on_callback:
                asyncio.ensure_future(self._safe_callback(frame))
            return

        req_id = (frame.get("headers") or {}).get("req_id", "")
        errcode = frame.get("errcode")

        # 鉴权应答
        if req_id.startswith(P.CMD_SUBSCRIBE):
            if errcode == 0:
                self.authenticated = True
                self.reconnect_attempts = 0
                self.last_error = None
                import time as _t

                self.last_auth_at = _t.time()
                self._authed_event.set()
                log.info("鉴权成功")
                self._start_heartbeat()
            else:
                self.authenticated = False
                self.last_error = f"鉴权失败 errcode={errcode} errmsg={frame.get('errmsg')}"
                log.error("%s", self.last_error)
            return

        # 心跳应答
        if req_id.startswith(P.CMD_PING):
            if errcode == 0:
                self.missed_pongs = 0
            else:
                log.warning("心跳回执异常 errcode=%s errmsg=%s", errcode, frame.get("errmsg"))
            return

        # 其它：回复 / 主动推送的回执
        q = self._waiters.get(req_id)
        if q:
            fut = q.popleft()
            if not q:
                self._waiters.pop(req_id, None)
            if not fut.done():
                if errcode == 0:
                    fut.set_result(frame)
                else:
                    fut.set_exception(AckError(int(errcode or -1), str(frame.get("errmsg"))))
            return

        log.debug("收到无人认领的帧：%s", json.dumps(frame, ensure_ascii=False)[:300])

    async def _safe_callback(self, frame: Dict[str, Any]) -> None:
        try:
            await self._on_callback(frame)  # type: ignore[misc]
        except Exception:
            log.exception("处理回调帧失败")

    # ---------- 心跳 ----------

    def _start_heartbeat(self) -> None:
        self._cancel_heartbeat()
        self._hb_task = asyncio.ensure_future(self._heartbeat_loop())

    def _cancel_heartbeat(self) -> None:
        if self._hb_task and not self._hb_task.done():
            self._hb_task.cancel()
        self._hb_task = None

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if self.missed_pongs >= 2:
                    log.warning("连续 %d 次心跳没有回执，主动断开重连", self.missed_pongs)
                    if self._ws is not None:
                        await self._ws.close()
                    return
                self.missed_pongs += 1
                try:
                    await self._send_raw(P.ping_frame())
                except Exception as e:
                    log.warning("心跳发送失败：%s", e)
                    return
        except asyncio.CancelledError:
            pass

    # ---------- 发送 ----------

    async def _send_raw(self, frame: Dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise NotConnected("长连接未建立")
        await ws.send(json.dumps(frame, ensure_ascii=False))

    async def send_and_wait(self, frame: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """发送一帧并等待同 req_id 的回执。

        同一 req_id 串行：流式回复会复用回调的 req_id，必须一条一条来，
        否则回执无法与请求一一对应。
        """
        if not self.authenticated:
            raise NotConnected("长连接未鉴权，无法发送")
        req_id = (frame.get("headers") or {}).get("req_id", "")
        if not req_id:
            raise ValueError("帧缺少 headers.req_id")
        lock = self._send_locks[req_id]
        async with lock:
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            self._waiters[req_id].append(fut)
            try:
                await self._send_raw(frame)
            except Exception:
                self._drop_waiter(req_id, fut)
                raise
            try:
                return await asyncio.wait_for(fut, timeout or self._ack_timeout)
            except asyncio.TimeoutError:
                self._drop_waiter(req_id, fut)
                raise TimeoutError(f"等待回执超时（{timeout or self._ack_timeout}s）req_id={req_id}")
            finally:
                if not self._waiters.get(req_id):
                    self._waiters.pop(req_id, None)
                    self._send_locks.pop(req_id, None)

    def _drop_waiter(self, req_id: str, fut: asyncio.Future) -> None:
        q = self._waiters.get(req_id)
        if q:
            try:
                q.remove(fut)
            except ValueError:
                pass
            if not q:
                self._waiters.pop(req_id, None)

    def _fail_all_waiters(self, reason: str) -> None:
        for req_id, q in list(self._waiters.items()):
            while q:
                fut = q.popleft()
                if not fut.done():
                    fut.set_exception(NotConnected(reason))
        self._waiters.clear()
        self._send_locks.clear()

    # ---------- 状态 ----------

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "authenticated": self.authenticated,
            "conn_gen": self.conn_gen,
            "reconnect_attempts": self.reconnect_attempts,
            "last_error": self.last_error,
            "last_auth_at": self.last_auth_at,
            "ws_url": self._ws_url,
        }
