"""企业微信智能机器人长连接协议常量与帧构造。

协议文档：https://developer.work.weixin.qq.com/document/path/101463
帧统一形如：{"cmd": ..., "headers": {"req_id": ...}, "body": {...}}
服务端应答形如：{"headers": {"req_id": ...}, "errcode": 0, "errmsg": "ok"}（无 cmd）。
"""

from __future__ import annotations

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
