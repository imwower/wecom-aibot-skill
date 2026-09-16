"""长连接客户端：鉴权、心跳、断线重连、回执路由。"""

from __future__ import annotations

import asyncio

import pytest

from wecom_bot import protocol as P
from wecom_bot.wsclient import AckError, NotConnected, WeComWsClient


async def _client(gw, **kw) -> WeComWsClient:
    c = WeComWsClient(gw.bot_id, gw.secret, ws_url=gw.url, **kw)
    c.start()
    return c


async def test_auth_success(gateway):
    c = await _client(gateway)
    try:
        assert await c.wait_authenticated(5)
        assert c.authenticated and c.connected
        assert gateway.auth_attempts[-1]["ok"] is True
        assert c.conn_gen == 1
    finally:
        await c.stop()


async def test_auth_failure_reports_errcode(gateway):
    c = WeComWsClient(gateway.bot_id, "wrong-secret", ws_url=gateway.url)
    c.start()
    try:
        assert not await c.wait_authenticated(2)
        assert c.authenticated is False
        assert "40001" in (c.last_error or "")
        assert gateway.auth_attempts[-1]["ok"] is False
    finally:
        await c.stop()


async def test_heartbeat_sent_and_acked(gateway):
    c = await _client(gateway, heartbeat_interval=0.15)
    try:
        assert await c.wait_authenticated(5)
        await asyncio.sleep(0.6)
        assert len(gateway.pings) >= 2
        assert c.missed_pongs == 0  # 收到回执后清零
    finally:
        await c.stop()


async def test_reconnect_after_drop(gateway):
    c = await _client(gateway, reconnect_base_delay=0.05, reconnect_max_delay=0.2)
    try:
        assert await c.wait_authenticated(5)
        gen1 = c.conn_gen
        await gateway.drop_connection()
        await asyncio.sleep(0.05)
        assert await c.wait_authenticated(5)
        assert c.conn_gen > gen1
        assert gateway.connections >= 2
    finally:
        await c.stop()


async def test_send_and_wait_routes_ack_by_req_id(gateway):
    c = await _client(gateway)
    try:
        assert await c.wait_authenticated(5)
        pushed = await gateway.push_text("hi")
        await asyncio.sleep(0.1)
        frame = P.respond_frame(pushed["req_id"], P.stream_body("S1", "第一段", finish=False))
        ack = await c.send_and_wait(frame)
        assert ack["errcode"] == 0
        assert gateway.streams[pushed["req_id"]].contents == ["第一段"]
    finally:
        await c.stop()


async def test_ack_error_raises(gateway):
    c = await _client(gateway)
    try:
        assert await c.wait_authenticated(5)
        # 用一个网关不认识的 req_id 回复 → errcode 40003
        frame = P.respond_frame("req_not_from_callback", P.stream_body("S", "x"))
        with pytest.raises(AckError) as ei:
            await c.send_and_wait(frame)
        assert ei.value.errcode == 40003
    finally:
        await c.stop()


async def test_send_before_auth_raises(gateway):
    c = WeComWsClient(gateway.bot_id, gateway.secret, ws_url=gateway.url)
    with pytest.raises(NotConnected):
        await c.send_and_wait(P.ping_frame())


async def test_serialised_replies_on_same_req_id(gateway):
    """流式回复复用同一个 req_id，必须串行且回执一一对应。"""
    c = await _client(gateway)
    try:
        assert await c.wait_authenticated(5)
        pushed = await gateway.push_text("hi")
        await asyncio.sleep(0.1)
        rid = pushed["req_id"]
        results = await asyncio.gather(
            c.send_and_wait(P.respond_frame(rid, P.stream_body("S", "a"))),
            c.send_and_wait(P.respond_frame(rid, P.stream_body("S", "ab"))),
            c.send_and_wait(P.respond_frame(rid, P.stream_body("S", "abc", finish=True))),
        )
        assert all(r["errcode"] == 0 for r in results)
        assert gateway.streams[rid].contents == ["a", "ab", "abc"]
        assert gateway.streams[rid].finished
    finally:
        await c.stop()
