"""daemon 端到端（进程内）：落库、5 秒确认、流式续写、超时回落、媒体解密、HTTP 接口。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wecom_bot import paths
from wecom_bot.config import Config
from wecom_bot.daemon import Daemon


def make_cfg(gw, **kw) -> Config:
    base = dict(
        bot_id=gw.bot_id,
        secret=gw.secret,
        ws_url=gw.url,
        http_port=0,
        heartbeat_interval=0.2,
        reconnect_base_delay=0.05,
        reconnect_max_delay=0.2,
        ack_timeout=3.0,
        ai_message_window=0,
    )
    base.update(kw)
    return Config(**base)


async def start_daemon(gw, **kw) -> Daemon:
    d = Daemon(make_cfg(gw, **kw))
    d.client.start()
    assert await d.client.wait_authenticated(5), "鉴权失败"
    return d


async def stop_daemon(d: Daemon) -> None:
    await d.client.stop()
    d.store.close()


# ---------------- 收消息 + 5 秒确认 ----------------


async def test_message_persisted_and_acked_within_5s(state_env, gateway):
    d = await start_daemon(gateway)
    try:
        t0 = time.time()
        pushed = await gateway.push_text("你好机器人")
        for _ in range(100):
            if gateway.responds:
                break
            await asyncio.sleep(0.05)
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"确认回复用了 {elapsed:.2f}s，超过 5 秒窗口"

        rows = d.store.list(limit=10)
        assert len(rows) == 1
        r = dict(rows[0])
        assert r["text"] == "你好机器人"
        assert r["msgid"] == pushed["msgid"]
        assert r["from_userid"] == "GeorgeTest"
        assert r["chattype"] == "single"
        assert r["chatid"] == "GeorgeTest"     # 单聊无 chatid 时用 userid
        assert r["is_read"] == 0
        assert r["stream_id"] and r["stream_finished"] == 0

        # 确认帧是未 finish 的流，后续可以续写
        body = gateway.responds[0]["body"]
        assert body["msgtype"] == "stream"
        assert body["stream"]["finish"] is False
        assert body["stream"]["content"] == d.cfg.ack_text
        assert gateway.responds[0]["headers"]["req_id"] == pushed["req_id"]

        assert d.store.last_chat()["chatid"] == "GeorgeTest"
    finally:
        await stop_daemon(d)


async def test_ack_disabled_when_ack_text_empty(state_env, gateway):
    d = await start_daemon(gateway, ack_text="")
    try:
        await gateway.push_text("不要确认")
        await asyncio.sleep(0.5)
        assert gateway.responds == []
        r = dict(d.store.list(limit=5)[0])
        assert r["stream_id"] is None
    finally:
        await stop_daemon(d)


async def test_duplicate_msgid_not_stored_twice(state_env, gateway):
    d = await start_daemon(gateway)
    try:
        body = {
            "msgid": "dup-1",
            "aibotid": gateway.bot_id,
            "chattype": "single",
            "from": {"userid": "U1"},
            "msgtype": "text",
            "text": {"content": "重复"},
        }
        await gateway.push_callback(body)
        await asyncio.sleep(0.3)
        await gateway.push_callback(dict(body))
        await asyncio.sleep(0.3)
        assert len(d.store.list(limit=10)) == 1
    finally:
        await stop_daemon(d)


async def test_event_callback_stored_without_reply(state_env, gateway):
    d = await start_daemon(gateway)
    try:
        await gateway.push_callback(
            {
                "msgid": "ev-1",
                "aibotid": gateway.bot_id,
                "create_time": 1700000000,
                "from": {"userid": "U2"},
                "msgtype": "event",
                "event": {"eventtype": "enter_chat"},
            },
            cmd="aibot_event_callback",
        )
        await asyncio.sleep(0.4)
        rows = [dict(r) for r in d.store.list(limit=10)]
        assert rows[0]["kind"] == "event"
        assert rows[0]["text"] == "[event:enter_chat]"
        assert gateway.responds == []   # 事件不自动回复
    finally:
        await stop_daemon(d)


# ---------------- 回复：续写流 / 回落主动推送 ----------------


async def test_reply_continues_stream(state_env, gateway):
    d = await start_daemon(gateway)
    try:
        pushed = await gateway.push_text("查一下天气")
        await asyncio.sleep(0.4)
        row = dict(d.store.list(limit=1)[0])

        res = await d.do_reply(str(row["id"]), "北京今天晴，25 度")
        assert res["route"] == "stream"

        st = gateway.streams[pushed["req_id"]]
        assert st.contents == [d.cfg.ack_text, "北京今天晴，25 度"]
        assert st.finished is True
        assert gateway.sends == []      # 没有走主动推送

        assert dict(d.store.get(str(row["id"])))["stream_finished"] == 1
    finally:
        await stop_daemon(d)


async def test_reply_falls_back_to_send_msg_when_stream_expired(state_env, gateway):
    d = await start_daemon(gateway, stream_window=0.2)
    try:
        await gateway.push_text("问题")
        await asyncio.sleep(0.4)
        row = dict(d.store.list(limit=1)[0])

        await asyncio.sleep(0.3)   # 超过 stream_window
        res = await d.do_reply(str(row["id"]), "迟到的答案")
        assert res["route"] == "send_msg"
        assert res["fallback_from"] == "stream"
        assert len(gateway.sends) == 1
        body = gateway.sends[0]["body"]
        assert body["chatid"] == "GeorgeTest"
        assert body["chat_type"] == 1
        assert body["markdown"]["content"] == "迟到的答案"
    finally:
        await stop_daemon(d)


async def test_reply_falls_back_when_stream_already_finished(state_env, gateway):
    d = await start_daemon(gateway)
    try:
        await gateway.push_text("问题")
        await asyncio.sleep(0.4)
        row_id = str(dict(d.store.list(limit=1)[0])["id"])
        assert (await d.do_reply(row_id, "第一次回答"))["route"] == "stream"
        # 同一条消息再回一次 → 流已 finish，只能主动推送
        assert (await d.do_reply(row_id, "补充一句"))["route"] == "send_msg"
        assert gateway.sends[0]["body"]["markdown"]["content"] == "补充一句"
    finally:
        await stop_daemon(d)


async def test_reply_falls_back_after_reconnect(state_env, gateway):
    """重连后旧 req_id 已失效，必须回落到主动推送。"""
    d = await start_daemon(gateway)
    try:
        await gateway.push_text("重连前的消息")
        await asyncio.sleep(0.4)
        row_id = str(dict(d.store.list(limit=1)[0])["id"])

        await gateway.drop_connection()
        await asyncio.sleep(0.1)
        assert await d.client.wait_authenticated(5)

        res = await d.do_reply(row_id, "重连后的回答")
        assert res["route"] == "send_msg"
        assert len(gateway.sends) == 1
    finally:
        await stop_daemon(d)


async def test_send_uses_last_chat_and_rejects_when_unknown(state_env, gateway):
    d = await start_daemon(gateway)
    try:
        with pytest.raises(RuntimeError, match="没有可用的会话"):
            await d.do_send("还没人说过话")

        await gateway.push_text("hi", userid="U9")
        await asyncio.sleep(0.4)
        res = await d.do_send("主动推送一条")
        assert res["chat_id"] == "U9"
        assert gateway.sends[-1]["body"]["markdown"]["content"] == "主动推送一条"
    finally:
        await stop_daemon(d)


async def test_send_reports_gateway_errcode(state_env, gateway):
    gateway.allow_send_msg = False
    d = await start_daemon(gateway)
    try:
        await gateway.push_text("hi")
        await asyncio.sleep(0.4)
        with pytest.raises(Exception) as ei:
            await d.do_send("会被拒绝")
        assert getattr(ei.value, "errcode", None) == 40006
    finally:
        await stop_daemon(d)


async def test_rate_limit_blocks_after_30_per_minute(state_env, gateway):
    d = await start_daemon(gateway)
    try:
        await gateway.push_text("hi")
        await asyncio.sleep(0.4)
        for i in range(30):
            d.store.add_sent(route="send_msg", chatid="U", reply_to=None,
                             msgtype="markdown", content=str(i), ok=True)
        with pytest.raises(RuntimeError, match="频率上限"):
            await d.do_send("第 31 条")
    finally:
        await stop_daemon(d)


# ---------------- 媒体下载解密 ----------------


def _encrypt(plain: bytes, key: bytes) -> bytes:
    pad = 16 - len(plain) % 16
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return enc.update(plain + bytes([pad]) * pad) + enc.finalize()


async def test_image_downloaded_and_decrypted(state_env, gateway, aiohttp_server=None):
    key = os.urandom(32)
    aeskey = base64.b64encode(key).decode()
    plain = b"\xff\xd8\xff\xe0FAKE-JPEG-BYTES" * 50
    cipher = _encrypt(plain, key)

    async def handler(request):
        return web.Response(
            body=cipher,
            headers={"Content-Disposition": 'attachment; filename="photo.jpg"'},
        )

    app = web.Application()
    app.router.add_get("/media", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        d = await start_daemon(gateway)
        try:
            await gateway.push_image(str(server.make_url("/media")), aeskey)
            for _ in range(100):
                row = dict(d.store.list(limit=1)[0]) if d.store.list(limit=1) else None
                if row and row.get("media_path"):
                    break
                await asyncio.sleep(0.05)
            row = dict(d.store.list(limit=1)[0])
            assert row["msgtype"] == "image"
            assert row["media_path"], "图片没有落地"
            saved = open(row["media_path"], "rb").read()
            assert saved == plain
            assert row["media_path"].endswith("photo.jpg")
            assert str(paths.media_dir()) in row["media_path"]
        finally:
            await stop_daemon(d)
    finally:
        await server.close()


# ---------------- HTTP 接口 ----------------


async def test_http_endpoints(state_env, gateway):
    d = await start_daemon(gateway)
    client = TestClient(TestServer(d.build_app()))
    await client.start_server()
    try:
        r = await client.get("/health")
        h = await r.json()
        assert h["ok"] and h["ws"]["authenticated"] is True
        assert h["bot_id"] == gateway.bot_id

        await gateway.push_text("第一条")
        await gateway.push_text("第二条")
        await asyncio.sleep(0.6)

        r = await client.get("/messages", params={"unread": "1"})
        data = await r.json()
        assert data["count"] == 2
        first_id = data["messages"][0]["id"]

        r = await client.post(f"/messages/{first_id}/read")
        assert (await r.json())["ok"] is True
        r = await client.get("/messages", params={"unread": "1"})
        assert (await r.json())["count"] == 1

        # /reply
        r = await client.post("/reply", json={"msg_id": str(first_id), "text": "回你"})
        body = await r.json()
        assert body["ok"] and body["route"] == "stream"

        # /send dry-run 不会真的发
        before = len(gateway.sends)
        r = await client.post("/send", json={"text": "预览", "dry_run": True})
        assert (await r.json())["dry_run"] is True
        assert len(gateway.sends) == before

        r = await client.post("/send", json={"text": "真发"})
        assert (await r.json())["ok"] is True
        assert gateway.sends[-1]["body"]["markdown"]["content"] == "真发"

        # /wait 在超时窗口内拿到新消息
        waiter = asyncio.ensure_future(
            client.get("/wait", params={"timeout": "5", "since_id": str(d.store.max_id())})
        )
        await asyncio.sleep(0.2)
        await gateway.push_text("等到的消息")
        resp = await waiter
        data = await resp.json()
        assert data["timeout"] is False
        assert data["messages"][0]["text"] == "等到的消息"

        # /wait 超时
        r = await client.get("/wait", params={"timeout": "0.5", "since_id": str(d.store.max_id())})
        assert (await r.json())["timeout"] is True

        # 错误路径
        r = await client.post("/reply", json={"msg_id": "99999", "text": "x"})
        assert r.status == 404
        r = await client.post("/send", json={"text": "  "})
        assert r.status == 400
    finally:
        await client.close()
        await stop_daemon(d)


async def test_http_token_required_when_configured(state_env, gateway):
    d = await start_daemon(gateway, http_token="tok123456")
    client = TestClient(TestServer(d.build_app()))
    await client.start_server()
    try:
        assert (await client.get("/health")).status == 401
        r = await client.get("/health", headers={"X-Wecom-Token": "tok123456"})
        assert r.status == 200
    finally:
        await client.close()
        await stop_daemon(d)
