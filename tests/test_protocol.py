"""协议帧与解析的单元测试：字段名必须与官方文档一致。"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wecom_bot import protocol as P
from wecom_bot.media import decrypt


def test_subscribe_frame_shape():
    f = P.subscribe_frame("BOT", "SEC")
    assert f["cmd"] == "aibot_subscribe"
    assert f["body"] == {"bot_id": "BOT", "secret": "SEC"}
    assert f["headers"]["req_id"].startswith("aibot_subscribe_")


def test_ping_frame_shape():
    f = P.ping_frame()
    assert f["cmd"] == "ping"
    assert f["headers"]["req_id"].startswith("ping_")
    assert "body" not in f


def test_stream_body_and_respond_frame():
    body = P.stream_body("SID", "内容", finish=True, feedback_id="FB")
    assert body == {
        "msgtype": "stream",
        "stream": {"id": "SID", "finish": True, "content": "内容", "feedback": {"id": "FB"}},
    }
    f = P.respond_frame("REQ", body)
    assert f["cmd"] == "aibot_respond_msg"
    assert f["headers"]["req_id"] == "REQ"  # 必须透传回调的 req_id


def test_send_msg_frame():
    f = P.send_msg_frame("CHAT", P.markdown_body("hi"), P.CHAT_TYPE_SINGLE)
    assert f["cmd"] == "aibot_send_msg"
    assert f["body"]["chatid"] == "CHAT"
    assert f["body"]["chat_type"] == 1
    assert f["body"]["msgtype"] == "markdown"
    assert f["body"]["markdown"]["content"] == "hi"


def test_chat_type_mapping():
    assert P.chat_type_from_chattype("single") == 1
    assert P.chat_type_from_chattype("group") == 2
    assert P.chat_type_from_chattype(None) is None


def test_extract_text():
    assert P.extract_text({"msgtype": "text", "text": {"content": "你好"}}) == "你好"
    assert P.extract_text({"msgtype": "image", "image": {"url": "u"}}) == "[image]"
    mixed = {
        "msgtype": "mixed",
        "mixed": {"msg_item": [{"msgtype": "text", "text": {"content": "看图"}}, {"msgtype": "image"}]},
    }
    assert P.extract_text(mixed) == "看图[图片]"
    assert P.extract_text({"msgtype": "event", "event": {"eventtype": "enter_chat"}}) == "[event:enter_chat]"


def test_media_ref():
    assert P.media_ref({"msgtype": "image", "image": {"url": "U", "aeskey": "K"}}) == {
        "kind": "image", "url": "U", "aeskey": "K"
    }
    assert P.media_ref({"msgtype": "text", "text": {"content": "x"}}) is None


def test_truncate_markdown():
    long = "啊" * 20000  # 60000 字节
    out = P.truncate_markdown(long)
    assert len(out.encode()) <= P.MARKDOWN_MAX_BYTES
    assert out.endswith("...")
    assert P.truncate_markdown("短") == "短"


def _encrypt(plain: bytes, key: bytes) -> bytes:
    pad = 16 - len(plain) % 16
    plain = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return enc.update(plain) + enc.finalize()


def test_decrypt_roundtrip():
    key = os.urandom(32)
    aeskey = base64.b64encode(key).decode().rstrip("=")  # 模拟无 padding 的 base64
    plain = b"hello \xe4\xbd\xa0\xe5\xa5\xbd" * 40
    assert decrypt(_encrypt(plain, key), aeskey) == plain


# ---------------- 图片消息与临时素材分片上传 ----------------


def test_image_body_and_send_frame():
    f = P.send_msg_frame("CHAT", P.image_body("MEDIA_ID"), P.CHAT_TYPE_SINGLE)
    assert f["cmd"] == "aibot_send_msg"
    assert f["body"] == {
        "chatid": "CHAT", "chat_type": 1,
        "msgtype": "image", "image": {"media_id": "MEDIA_ID"},
    }


def test_upload_frames_shape():
    init = P.upload_media_init_frame("image", "a.png", 1000, 1, "MD5")
    assert init["cmd"] == "aibot_upload_media_init"
    assert init["body"] == {
        "type": "image", "filename": "a.png", "total_size": 1000,
        "total_chunks": 1, "md5": "MD5",
    }
    chunk = P.upload_media_chunk_frame("UP", 0, b"ab")
    assert chunk["cmd"] == "aibot_upload_media_chunk"
    assert chunk["body"] == {"upload_id": "UP", "chunk_index": 0, "base64_data": "YWI="}
    fin = P.upload_media_finish_frame("UP")
    assert fin["cmd"] == "aibot_upload_media_finish"
    assert fin["body"] == {"upload_id": "UP"}


def test_media_upload_plan_and_split():
    data = b"\x89PNG" + os.urandom(P.MEDIA_CHUNK_BYTES * 2 + 7)
    chunks = P.split_chunks(data)
    assert len(chunks) == 3
    assert all(len(c) <= P.MEDIA_CHUNK_BYTES for c in chunks)
    assert b"".join(chunks) == data
    plan = P.media_upload_plan(data, "big.png")
    assert plan["type"] == "image"
    assert plan["total_size"] == len(data)
    assert plan["total_chunks"] == 3
    assert len(plan["md5"]) == 32


def test_validate_media_rules():
    import pytest

    P.validate_media(b"x" * 100, "ok.png", "image")
    with pytest.raises(ValueError):
        P.validate_media(b"x" * 100, "bad.bmp", "image")       # 扩展名不支持
    with pytest.raises(ValueError):
        P.validate_media(b"x", "tiny.png", "image")            # 小于 5 字节
    with pytest.raises(ValueError):
        P.validate_media(b"x" * (10 * 1024 * 1024 + 1), "big.png", "image")  # 超 10MB
