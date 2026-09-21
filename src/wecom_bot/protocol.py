"""企业微信智能机器人长连接协议常量与帧构造。

协议文档：https://developer.work.weixin.qq.com/document/path/101463
帧统一形如：{"cmd": ..., "headers": {"req_id": ...}, "body": {...}}
服务端应答形如：{"headers": {"req_id": ...}, "errcode": 0, "errmsg": "ok"}（无 cmd）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from typing import Any, Dict, Optional

WsFrame = Dict[str, Any]

# ---- 开发者 → 企业微信 ----
CMD_SUBSCRIBE = "aibot_subscribe"          # 鉴权订阅
CMD_PING = "ping"                          # 心跳
CMD_RESPOND_MSG = "aibot_respond_msg"      # 回复消息（含流式）
CMD_RESPOND_WELCOME = "aibot_respond_welcome_msg"
CMD_RESPOND_UPDATE = "aibot_respond_update_msg"
CMD_SEND_MSG = "aibot_send_msg"            # 主动推送
CMD_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"      # 分片上传：初始化
CMD_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"    # 分片上传：传分片
CMD_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"  # 分片上传：合并取 media_id

# ---- 企业微信 → 开发者 ----
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"

# chat_type 取值（aibot_send_msg）
CHAT_TYPE_SINGLE = 1
CHAT_TYPE_GROUP = 2

# 回调里的 chattype 字面量
CHATTYPE_SINGLE = "single"
CHATTYPE_GROUP = "group"

# 已知限制（秒）
REPLY_DEADLINE = 5.0          # 收到回调后需在 5 秒内回复
STREAM_MAX_WINDOW = 600.0     # 流式消息 10 分钟内必须 finish
MARKDOWN_MAX_BYTES = 20480    # markdown content 上限

# 临时素材分片上传（官方：单片 512KB（base64 前），最多 100 片，素材 3 天有效）
MEDIA_CHUNK_BYTES = 512 * 1024
MEDIA_MAX_CHUNKS = 100
MEDIA_MIN_BYTES = 5
MEDIA_MAX_BYTES = {
    "image": 10 * 1024 * 1024,
    "voice": 2 * 1024 * 1024,
    "video": 10 * 1024 * 1024,
    "file": 20 * 1024 * 1024,
}
MEDIA_EXTS = {
    "image": {".png", ".jpg", ".jpeg", ".gif"},
    "voice": {".amr"},
    "video": {".mp4"},
}


def generate_req_id(prefix: str) -> str:
    """生成请求 ID，格式 {prefix}_{毫秒时间戳}_{随机}。

    官方 SDK 依赖前缀来区分鉴权 / 心跳应答，这里保持一致。
    """
    return f"{prefix}_{int(time.time() * 1000)}_{os.urandom(4).hex()}"


def subscribe_frame(bot_id: str, secret: str, req_id: Optional[str] = None) -> WsFrame:
    return {
        "cmd": CMD_SUBSCRIBE,
        "headers": {"req_id": req_id or generate_req_id(CMD_SUBSCRIBE)},
        "body": {"bot_id": bot_id, "secret": secret},
    }


def ping_frame(req_id: Optional[str] = None) -> WsFrame:
    return {"cmd": CMD_PING, "headers": {"req_id": req_id or generate_req_id(CMD_PING)}}


def stream_body(
    stream_id: str,
    content: str,
    finish: bool = False,
    feedback_id: Optional[str] = None,
) -> Dict[str, Any]:
    stream: Dict[str, Any] = {"id": stream_id, "finish": finish, "content": content}
    if feedback_id:
        stream["feedback"] = {"id": feedback_id}
    return {"msgtype": "stream", "stream": stream}


def markdown_body(content: str) -> Dict[str, Any]:
    return {"msgtype": "markdown", "markdown": {"content": content}}


def text_body(content: str) -> Dict[str, Any]:
    return {"msgtype": "text", "text": {"content": content}}


def image_body(media_id: str) -> Dict[str, Any]:
    return {"msgtype": "image", "image": {"media_id": media_id}}


def media_body(kind: str, media_id: str, **extra: Any) -> Dict[str, Any]:
    """file / image / voice / video 统一形如 {"msgtype": kind, kind: {"media_id": ...}}。"""
    node: Dict[str, Any] = {"media_id": media_id}
    node.update({k: v for k, v in extra.items() if v is not None})
    return {"msgtype": kind, kind: node}


def upload_media_init_frame(
    kind: str,
    filename: str,
    total_size: int,
    total_chunks: int,
    md5: Optional[str] = None,
    req_id: Optional[str] = None,
) -> WsFrame:
    body: Dict[str, Any] = {
        "type": kind,
        "filename": filename,
        "total_size": total_size,
        "total_chunks": total_chunks,
    }
    if md5:
        body["md5"] = md5
    return {
        "cmd": CMD_UPLOAD_MEDIA_INIT,
        "headers": {"req_id": req_id or generate_req_id(CMD_UPLOAD_MEDIA_INIT)},
        "body": body,
    }


def upload_media_chunk_frame(
    upload_id: str, chunk_index: int, chunk: bytes, req_id: Optional[str] = None
) -> WsFrame:
    return {
        "cmd": CMD_UPLOAD_MEDIA_CHUNK,
        "headers": {"req_id": req_id or generate_req_id(CMD_UPLOAD_MEDIA_CHUNK)},
        "body": {
            "upload_id": upload_id,
            "chunk_index": chunk_index,
            "base64_data": base64.b64encode(chunk).decode("ascii"),
        },
    }


def upload_media_finish_frame(upload_id: str, req_id: Optional[str] = None) -> WsFrame:
    return {
        "cmd": CMD_UPLOAD_MEDIA_FINISH,
        "headers": {"req_id": req_id or generate_req_id(CMD_UPLOAD_MEDIA_FINISH)},
        "body": {"upload_id": upload_id},
    }


def split_chunks(data: bytes, chunk_size: int = MEDIA_CHUNK_BYTES) -> list:
    """按 base64 编码前的字节切片；空文件返回一个空片以保证 total_chunks>=1。"""
    if not data:
        return [b""]
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


def media_upload_plan(
    data: bytes, filename: str, kind: str = "image", chunk_size: int = MEDIA_CHUNK_BYTES
) -> Dict[str, Any]:
    """按官方字段算出 init 需要的参数（不含 base64 内容，可直接打印给用户看）。"""
    chunks = split_chunks(data, chunk_size)
    return {
        "type": kind,
        "filename": filename,
        "total_size": len(data),
        "total_chunks": len(chunks),
        "md5": hashlib.md5(data).hexdigest(),
    }


def validate_media(data: bytes, filename: str, kind: str = "image") -> None:
    """按官方限制做本地预检，避免白跑一趟长连接。"""
    if kind not in MEDIA_MAX_BYTES:
        raise ValueError(f"不支持的素材类型 {kind}（支持 image/voice/video/file）")
    exts = MEDIA_EXTS.get(kind)
    if exts:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in exts:
            raise ValueError(f"{kind} 只支持 {'/'.join(sorted(exts))}，拿到的是 {ext or '无扩展名'}")
    if len(data) < MEDIA_MIN_BYTES:
        raise ValueError(f"文件太小（{len(data)} 字节），至少 {MEDIA_MIN_BYTES} 字节")
    limit = MEDIA_MAX_BYTES[kind]
    if len(data) > limit:
        raise ValueError(f"{kind} 不能超过 {limit // (1024 * 1024)}MB，当前 {len(data) / 1048576:.2f}MB")
    total_chunks = len(split_chunks(data))
    if total_chunks > MEDIA_MAX_CHUNKS:
        raise ValueError(f"分片数 {total_chunks} 超过上限 {MEDIA_MAX_CHUNKS}")


def respond_frame(req_id: str, body: Dict[str, Any], cmd: str = CMD_RESPOND_MSG) -> WsFrame:
    """回复帧必须透传收到的 req_id，企微据此把回复挂到原会话上。"""
    return {"cmd": cmd, "headers": {"req_id": req_id}, "body": body}


def send_msg_frame(
    chatid: str,
    body: Dict[str, Any],
    chat_type: Optional[int] = None,
    req_id: Optional[str] = None,
) -> WsFrame:
    full: Dict[str, Any] = {"chatid": chatid}
    if chat_type:
        full["chat_type"] = chat_type
    full.update(body)
    return {
        "cmd": CMD_SEND_MSG,
        "headers": {"req_id": req_id or generate_req_id(CMD_SEND_MSG)},
        "body": full,
    }


def chat_type_from_chattype(chattype: Optional[str]) -> Optional[int]:
    if chattype == CHATTYPE_SINGLE:
        return CHAT_TYPE_SINGLE
    if chattype == CHATTYPE_GROUP:
        return CHAT_TYPE_GROUP
    return None


def truncate_markdown(content: str, limit: int = MARKDOWN_MAX_BYTES) -> str:
    """按 UTF-8 字节数截断，避免超过 20480 字节上限。"""
    raw = content.encode("utf-8")
    if len(raw) <= limit:
        return content
    return raw[: limit - 3].decode("utf-8", errors="ignore") + "..."


def extract_text(body: Dict[str, Any]) -> str:
    """从回调 body 里提取可读文本，供收件箱展示。"""
    msgtype = body.get("msgtype")
    if msgtype == "text":
        return str(body.get("text", {}).get("content", ""))
    if msgtype == "markdown":
        return str(body.get("markdown", {}).get("content", ""))
    if msgtype == "mixed":
        parts = []
        for item in body.get("mixed", {}).get("msg_item", []) or []:
            if item.get("msgtype") == "text":
                parts.append(str(item.get("text", {}).get("content", "")))
            elif item.get("msgtype") == "image":
                parts.append("[图片]")
            else:
                parts.append(f"[{item.get('msgtype')}]")
        return "".join(parts)
    if msgtype == "event":
        ev = body.get("event", {}) or {}
        return f"[event:{ev.get('eventtype', 'unknown')}]"
    if msgtype == "voice" and body.get('voice', {}).get('content'):
        return str(body['voice']['content'])
    if msgtype in ("image", "voice", "file", "video"):
        return f"[{msgtype}]"
    return f"[{msgtype}]" if msgtype else ""


def media_ref(body: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """取出可下载的媒体引用 {url, aeskey, kind}；没有则 None。"""
    for kind in ("image", "voice", "file", "video"):
        node = body.get(kind)
        if isinstance(node, dict) and node.get("url"):
            return {"kind": kind, "url": node["url"], "aeskey": node.get("aeskey", "")}
    return None


def media_refs(body: Dict[str, Any]) -> list:
    """附件、图文混排图片和引用附件；保持消息顺序。"""
    refs = []
    ref = media_ref(body)
    if ref:
        refs.append(ref)
    for item in (body.get('mixed') or {}).get('msg_item', []) or []:
        ref = media_ref(item)
        if ref:
            refs.append(ref)
    quote = body.get('quote')
    if isinstance(quote, dict):
        refs.extend(media_refs({k: v for k, v in quote.items() if k != 'quote'}))
    return refs
