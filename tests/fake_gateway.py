"""模拟企业微信长连接网关（按官方文档的帧格式实现，供测试使用）。

支持：
  - aibot_subscribe 鉴权（校验 bot_id / secret）
  - ping 心跳
  - 主动下推 aibot_msg_callback / aibot_event_callback
  - 校验 aibot_respond_msg（含流式 stream id / finish / 10 分钟窗口）
  - 校验 aibot_send_msg（主动推送）
  - 模拟「同一 bot 只能一条长连接」：新连接踢掉旧连接
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import websockets
from websockets.asyncio.server import ServerConnection, serve

# 与官方错误码不是一一对应，仅用于测试区分失败原因
ERR_AUTH = 40001
ERR_BAD_REQ_ID = 40003
ERR_STREAM_FINISHED = 40004
ERR_STREAM_EXPIRED = 40005
ERR_NO_INTERACTION = 40006


@dataclass
class StreamState:
    stream_id: str
    opened_at: float
    finished: bool = False
    contents: List[str] = field(default_factory=list)


class FakeGateway:
    def __init__(
        self,
        bot_id: str = "aibTESTBOTID",
        secret: str = "test-secret-0123456789",
        *,
        stream_window: float = 600.0,
        allow_send_msg: bool = True,
    ) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.stream_window = stream_window
        self.allow_send_msg = allow_send_msg

        self.host = "127.0.0.1"
        self.port = 0
        self._server = None
        self._conn: Optional[ServerConnection] = None

        # 记录
        self.frames: List[Dict[str, Any]] = []       # 收到的全部帧
        self.responds: List[Dict[str, Any]] = []     # aibot_respond_msg
        self.sends: List[Dict[str, Any]] = []        # aibot_send_msg
        self.pings: List[Dict[str, Any]] = []
        self.auth_attempts: List[Dict[str, Any]] = []
        self.connections = 0
        self.kicked = 0

        # req_id -> 流状态
        self.streams: Dict[str, StreamState] = {}
        self.callback_req_ids: set = set()
        self.authed = asyncio.Event()
        self.any_frame = asyncio.Event()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def start(self) -> "FakeGateway":
        self._server = await serve(self._handler, self.host, 0, ping_interval=None)
        self.port = list(self._server.sockets)[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ---------- 连接处理 ----------

    async def _handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        if self._conn is not None:
            # 同一 bot 只能一条长连接：踢掉旧的
            self.kicked += 1
            old = self._conn
            self._conn = None
            try:
                await old.close(code=4001, reason="kicked by new connection")
            except Exception:
                pass
        self._conn = ws
        try:
            async for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                self.any_frame.set()
                await self._dispatch(ws, frame)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if self._conn is ws:
                self._conn = None
                self.authed.clear()

    async def _reply(self, ws: ServerConnection, req_id: str, errcode: int = 0, errmsg: str = "ok",
                     body: Optional[Dict[str, Any]] = None) -> None:
        frame: Dict[str, Any] = {"headers": {"req_id": req_id}, "errcode": errcode, "errmsg": errmsg}
        if body is not None:
            frame["body"] = body
        await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _dispatch(self, ws: ServerConnection, frame: Dict[str, Any]) -> None:
        cmd = frame.get("cmd")
        req_id = (frame.get("headers") or {}).get("req_id", "")
        body = frame.get("body") or {}

        if cmd == "aibot_subscribe":
            self.auth_attempts.append({"bot_id": body.get("bot_id"), "ok": None})
            if body.get("bot_id") == self.bot_id and body.get("secret") == self.secret:
                self.auth_attempts[-1]["ok"] = True
                self.authed.set()
                await self._reply(ws, req_id, 0, "ok")
            else:
                self.auth_attempts[-1]["ok"] = False
                await self._reply(ws, req_id, ERR_AUTH, "invalid bot_id or secret")
            return

        if cmd == "ping":
            self.pings.append(frame)
            await self._reply(ws, req_id, 0, "ok")
            return

        if cmd == "aibot_respond_msg":
            self.responds.append(frame)
            if req_id not in self.callback_req_ids:
                await self._reply(ws, req_id, ERR_BAD_REQ_ID, "unknown req_id")
                return
            if body.get("msgtype") == "stream":
                st = body.get("stream") or {}
                sid = st.get("id")
                cur = self.streams.get(req_id)
                if cur is None:
                    cur = StreamState(stream_id=sid, opened_at=time.time())
                    self.streams[req_id] = cur
                else:
                    if cur.finished:
                        await self._reply(ws, req_id, ERR_STREAM_FINISHED, "stream already finished")
                        return
                    if time.time() - cur.opened_at > self.stream_window:
                        await self._reply(ws, req_id, ERR_STREAM_EXPIRED, "stream expired")
                        return
                cur.contents.append(st.get("content", ""))
                if st.get("finish"):
                    cur.finished = True
            await self._reply(ws, req_id, 0, "ok")
            return

        if cmd == "aibot_send_msg":
            self.sends.append(frame)
            if not self.allow_send_msg:
                await self._reply(ws, req_id, ERR_NO_INTERACTION, "user has not interacted")
                return
            if not body.get("chatid"):
                await self._reply(ws, req_id, 40007, "missing chatid")
                return
            await self._reply(ws, req_id, 0, "ok")
            return

        await self._reply(ws, req_id, 40000, f"unsupported cmd {cmd}")

    # ---------- 测试辅助 ----------

    async def wait_authed(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.authed.wait(), timeout)

    async def push_text(self, content: str, *, userid: str = "GeorgeTest",
                        chatid: Optional[str] = None, chattype: str = "single") -> Dict[str, str]:
        body: Dict[str, Any] = {
            "msgid": f"msg_{uuid.uuid4().hex[:12]}",
            "aibotid": self.bot_id,
            "chattype": chattype,
            "from": {"userid": userid},
            "msgtype": "text",
            "text": {"content": content},
        }
        if chatid:
            body["chatid"] = chatid
        return await self.push_callback(body)

    async def push_image(self, url: str, aeskey: str, *, userid: str = "GeorgeTest") -> Dict[str, str]:
        body = {
            "msgid": f"msg_{uuid.uuid4().hex[:12]}",
            "aibotid": self.bot_id,
            "chattype": "single",
            "from": {"userid": userid},
            "msgtype": "image",
            "image": {"url": url, "aeskey": aeskey},
        }
        return await self.push_callback(body)

    async def push_callback(self, body: Dict[str, Any], cmd: str = "aibot_msg_callback") -> Dict[str, str]:
        if self._conn is None:
            raise RuntimeError("还没有客户端连上来")
        req_id = f"cb_{uuid.uuid4().hex[:12]}"
        self.callback_req_ids.add(req_id)
        await self._conn.send(
            json.dumps({"cmd": cmd, "headers": {"req_id": req_id}, "body": body}, ensure_ascii=False)
        )
        return {"req_id": req_id, "msgid": body.get("msgid", "")}

    async def drop_connection(self) -> None:
        """模拟网络抖动：直接断开当前连接。"""
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            self.authed.clear()
            await conn.close(code=4000, reason="drop")

    def expire_stream(self, req_id: str, seconds: float) -> None:
        """把某个流的开启时间往前拨，模拟超过 10 分钟窗口。"""
        st = self.streams.get(req_id)
        if st:
            st.opened_at -= seconds
