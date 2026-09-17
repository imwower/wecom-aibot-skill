"""常驻进程：维护唯一一条企微长连接，落库收件箱，并对外暴露本地 HTTP 接口。

因为同一个 bot 只能有一条长连接，所有发送都必须经由本进程。
HTTP 只监听 127.0.0.1。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

from . import __version__, paths, protocol as P
from .config import Config, load as load_config
from .lockfile import ProcessLock
from .logging_setup import setup as setup_logging
from .media import download_and_decrypt
from .store import Store, row_to_dict
from .wsclient import AckError, NotConnected, WeComWsClient
from .automation import Automation, accepted_prompt

log = logging.getLogger("wecom.daemon")

# 官方限制：回复 + 主动推送合计 30 条/分钟
RATE_LIMIT_PER_MIN = 30


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.started_at = time.time()
        self.store = Store(paths.inbox_path())
        self.new_message = asyncio.Event()
        self.client = WeComWsClient(
            cfg.bot_id,
            cfg.secret,
            ws_url=cfg.ws_url,
            heartbeat_interval=cfg.heartbeat_interval,
            reconnect_base_delay=cfg.reconnect_base_delay,
            reconnect_max_delay=cfg.reconnect_max_delay,
            ack_timeout=cfg.ack_timeout,
            on_callback=self.on_callback,
        )
        self._runner: Optional[web.AppRunner] = None
        self._stop_event = asyncio.Event()
        self.automation = Automation(self) if cfg.auto_enabled else None
        self._attachment_tasks = set()

    # ---------- 收消息 ----------

    async def on_callback(self, frame: Dict[str, Any]) -> None:
        body: Dict[str, Any] = frame.get("body") or {}
        req_id = (frame.get("headers") or {}).get("req_id", "")
        kind = "event" if frame.get("cmd") == P.CMD_EVENT_CALLBACK else "message"
        msgid = body.get("msgid")
        text = P.extract_text(body)
        auto_prompt = accepted_prompt(self.cfg, body, kind)

        # 单聊回调可能没有 chatid，此时 chatid 即用户 userid
        from_userid = (body.get("from") or {}).get("userid")
        chatid = body.get("chatid") or from_userid
        chattype = body.get("chattype") or (P.CHATTYPE_SINGLE if not body.get("chatid") else None)
        if chatid:
            body.setdefault("chatid", chatid)
            if chattype:
                body.setdefault("chattype", chattype)

        row_id = self.store.add_message(
            msgid=msgid,
            req_id=req_id,
            kind=kind,
            body=body,
            text=text,
            conn_gen=self.client.conn_gen,
        )
        if row_id is None:
            log.info("重复推送，已忽略 msgid=%s", msgid)
        else:
            log.info(
                "收到%s id=%s msgtype=%s chat=%s from=%s text=%s",
                "事件" if kind == "event" else "消息",
                row_id,
                body.get("msgtype"),
                chatid,
                from_userid,
                (text or "")[:80],
            )
            self.store.remember_chat(chatid, chattype)
            self.new_message.set()
            self.new_message.clear()

        if self.automation:
            if row_id is not None and auto_prompt is not None:
                self.automation.jobs.enqueue(row_id, body, auto_prompt)
                refs = P.media_refs(body)
                needs_media = bool(refs) or body.get('msgtype') in ('file', 'image', 'video')
                if needs_media:
                    self.automation.jobs.update(row_id, 'downloading')
                try:
                    if not self._rate_limited():
                        summary = ' '.join(auto_prompt.split())
                        prefix, suffix = '任务已接单：（', '），完成后通知你。'
                        budget = 100 - len(prefix) - len(suffix)
                        if len(summary) > budget:
                            summary = summary[:budget - 1] + '…'
                        ack_text = prefix + summary + suffix
                        frame = P.respond_frame(req_id, P.stream_body(P.generate_req_id('ack'),
                            ack_text, finish=True))
                        await self.client.send_and_wait(frame)
                        self.store.add_sent(route='stream', chatid=chatid, reply_to=msgid,
                            msgtype='stream', content=ack_text, ok=True)
                except Exception:
                    log.warning('任务 #%s 接单回执未确认；任务已持久化，不重复发送回执', row_id)
                finally:
                    if needs_media:
                        task = asyncio.create_task(self._prepare_attachments(row_id, refs))
                        self._attachment_tasks.add(task)
                        task.add_done_callback(self._attachment_tasks.discard)
                    else:
                        self.automation.jobs.update(row_id, 'queued')
            return

        # 先在 5 秒窗口内回确认（同时把流开起来，后续 reply 可以续写）
        if kind == "message" and self.cfg.ack_text and row_id is not None:
            await self._send_ack(row_id, req_id)

        # 媒体下载放到 ack 之后，避免占用 5 秒窗口；url 只有 5 分钟有效期
        if row_id is not None and self.cfg.download_media:
            ref = P.media_ref(body)
            if ref:
                asyncio.ensure_future(self._fetch_media(row_id, ref, msgid or str(row_id)))

    async def _prepare_attachments(self, row_id, refs):
        jobs = self.automation.jobs
        async def download():
            files = []
            for index, ref in enumerate(refs):
                path = await download_and_decrypt(ref['url'], ref.get('aeskey', ''), paths.media_dir(),
                    kind=ref['kind'], msgid=f'{row_id}-{index}')
                files.append({'path': str(path), 'kind': ref['kind']})
            return files
        try:
            if not self.cfg.download_media or not refs or len(refs) > 10:
                raise ValueError('attachments unavailable')
            files = await asyncio.wait_for(download(), 120)
            jobs.attach(row_id, files)
            self.store.set_media_path(row_id, files[0]['path'])
            jobs.update(row_id, 'queued')
        except (Exception, asyncio.CancelledError) as exc:
            jobs.execution(row_id, outcome='blocked', finished_at=time.time(), error_kind='attachment_download')
            jobs.update(row_id, 'ready', '附件下载未完成（链接可能已失效、文件过大或下载被关闭），请重新发送；本次未执行附件中的任何操作。')
            # 不记录含签名的下载链接或解密密钥。
            log.warning('附件 #%s 下载失败，类型=%s', row_id, type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _send_ack(self, row_id: int, req_id: str) -> None:
        stream_id = P.generate_req_id("stream")
        frame = P.respond_frame(req_id, P.stream_body(stream_id, self.cfg.ack_text, finish=False))
        opened_at = time.time()
        try:
            await self.client.send_and_wait(frame, timeout=self.cfg.ack_timeout)
        except Exception as e:
            log.warning("确认回复失败（id=%s）：%s", row_id, e)
            self.store.add_sent(
                route="stream",
                chatid=None,
                reply_to=str(row_id),
                msgtype="stream",
                content=self.cfg.ack_text,
                ok=False,
                errmsg=str(e),
            )
            return
        self.store.set_stream(row_id, stream_id, opened_at, self.client.conn_gen)
        self.store.add_sent(
            route="stream",
            chatid=None,
            reply_to=str(row_id),
            msgtype="stream",
            content=self.cfg.ack_text,
            ok=True,
        )
        log.info("已在 %.2fs 内回确认，stream_id=%s", time.time() - opened_at, stream_id)

    async def _fetch_media(self, row_id: int, ref: Dict[str, str], msgid: str) -> None:
        try:
            path = await download_and_decrypt(
                ref["url"], ref.get("aeskey", ""), paths.media_dir(), kind=ref["kind"], msgid=msgid
            )
            self.store.set_media_path(row_id, str(path))
        except Exception as e:
            log.warning("媒体下载失败（id=%s kind=%s）：%s", row_id, ref["kind"], e)

    # ---------- 发消息 ----------

    def _rate_limited(self) -> bool:
        return self.store.sent_count_since(time.time() - 60) >= RATE_LIMIT_PER_MIN

    async def do_reply(self, ident: str, text: str) -> Dict[str, Any]:
        """回复某条消息：优先续写它的流；流不可用则回落到主动推送。"""
        row = self.store.get(ident)
        if row is None:
            raise KeyError(f"收件箱里没有 id/msgid = {ident}")
        if self._rate_limited():
            raise RuntimeError("达到 30 条/分钟的发送频率上限，稍后再试")

        d = dict(row)
        now = time.time()
        stream_ok = (
            bool(d.get("stream_id"))
            and not d.get("stream_finished")
            and d.get("stream_opened_at")
            and now - float(d["stream_opened_at"]) < self.cfg.stream_window
            and d.get("conn_gen") == self.client.conn_gen
            and self.client.authenticated
        )
        if stream_ok:
            frame = P.respond_frame(
                d["req_id"], P.stream_body(d["stream_id"], P.truncate_markdown(text), finish=True)
            )
            try:
                await self.client.send_and_wait(frame)
                self.store.finish_stream(int(d["id"]))
                self.store.add_sent(
                    route="stream",
                    chatid=d.get("chatid"),
                    reply_to=d.get("msgid"),
                    msgtype="stream",
                    content=text,
                    ok=True,
                )
                return {"route": "stream", "stream_id": d["stream_id"], "id": d["id"]}
            except (AckError, TimeoutError, NotConnected) as e:
                log.warning("续写流失败，回落到主动推送：%s", e)
                self.store.finish_stream(int(d["id"]))

        chatid = d.get("chatid")
        if not chatid:
            raise RuntimeError("这条消息没有 chatid，无法回落到主动推送")
        res = await self.do_send(text, chat_id=chatid, chattype=d.get("chattype"), reply_to=d.get("msgid"))
        res["fallback_from"] = "stream"
        return res

    async def do_send(
        self,
        text: str,
        *,
        chat_id: Optional[str] = None,
        chattype: Optional[str] = None,
        markdown: bool = True,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """主动推送（aibot_send_msg）。需要用户此前与机器人互动过。"""
        if not chat_id:
            last = self.store.last_chat() or {}
            chat_id = last.get("chatid")
            chattype = chattype or last.get("chattype")
            if not chat_id:
                raise RuntimeError(
                    "没有可用的会话：还没有人和机器人说过话，也没有指定 --to <chat_id>"
                )
        if self._rate_limited():
            raise RuntimeError("达到 30 条/分钟的发送频率上限，稍后再试")
        if not self.client.authenticated:
            raise NotConnected("长连接未就绪，无法发送")

        content = P.truncate_markdown(text)
        # aibot_send_msg 只支持 markdown / template_card，纯文本也走 markdown
        body = P.markdown_body(content)
        frame = P.send_msg_frame(chat_id, body, P.chat_type_from_chattype(chattype))
        try:
            await self.client.send_and_wait(frame)
        except Exception as e:
            errcode = getattr(e, "errcode", None)
            self.store.add_sent(
                route="send_msg",
                chatid=chat_id,
                reply_to=reply_to,
                msgtype="markdown",
                content=text,
                ok=False,
                errcode=errcode,
                errmsg=str(e),
            )
            raise
        self.store.add_sent(
            route="send_msg",
            chatid=chat_id,
            reply_to=reply_to,
            msgtype="markdown",
            content=text,
            ok=True,
        )
        return {"route": "send_msg", "chat_id": chat_id, "chat_type": chattype}

    # ---------- HTTP ----------

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/health", self.h_health)
        app.router.add_get("/messages", self.h_messages)
        app.router.add_post("/messages/{ident}/read", self.h_mark_read)
        app.router.add_get("/wait", self.h_wait)
        app.router.add_post("/send", self.h_send)
        app.router.add_post("/reply", self.h_reply)
        app.router.add_post("/shutdown", self.h_shutdown)
        return app

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self.cfg.http_token:
            if request.headers.get("X-Wecom-Token") != self.cfg.http_token:
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def h_health(self, request: web.Request) -> web.Response:
        last = self.store.last_chat()
        return web.json_response(
            {
                "ok": True,
                "version": __version__,
                "pid": os.getpid(),
                "uptime": round(time.time() - self.started_at, 1),
                "bot_id": self.cfg.bot_id,
                "ws": self.client.status(),
                "unread": self.store.unread_count(),
                "max_id": self.store.max_id(),
                "last_chat": last,
                "sent_last_minute": self.store.sent_count_since(time.time() - 60),
                "inbox": str(paths.inbox_path()),
                "log": str(paths.daemon_log_path()),
                "automation": {
                    "enabled": self.cfg.auto_enabled,
                    "provider": self.cfg.ai_provider,
                    "full_access": self.cfg.ai_full_access,
                    "owner_configured": bool(self.cfg.owner_userid),
                    "tasks": self.automation.jobs.status() if self.automation else {},
                    "outcomes": self.automation.jobs.outcomes() if self.automation else {},
                    "worker_alive": bool(self.automation and self.automation.task and not self.automation.task.done()),
                },
            }
        )

    async def h_messages(self, request: web.Request) -> web.Response:
        q = request.query
        rows = self.store.list(
            since=_float_or_none(q.get("since")),
            since_id=_int_or_none(q.get("since_id")),
            unread_only=q.get("unread") in ("1", "true", "yes"),
            limit=int(q.get("limit", 50)),
            kind=q.get("kind"),
        )
        include_raw = q.get("raw") in ("1", "true", "yes")
        items = [row_to_dict(r, include_raw) for r in rows]
        if q.get("mark_read") in ("1", "true", "yes"):
            self.store.mark_read([int(i["id"]) for i in items])
            for i in items:
                i["is_read"] = True
        return web.json_response({"messages": items, "count": len(items)})

    async def h_mark_read(self, request: web.Request) -> web.Response:
        ident = request.match_info["ident"]
        row = self.store.get(ident)
        if row is None:
            return web.json_response({"error": f"没有 id/msgid = {ident}"}, status=404)
        self.store.mark_read([int(row["id"])])
        return web.json_response({"ok": True, "id": row["id"]})

    async def h_wait(self, request: web.Request) -> web.Response:
        timeout = float(request.query.get("timeout", 300))
        since_id = _int_or_none(request.query.get("since_id"))
        unread_only = request.query.get("unread", "1") in ("1", "true", "yes")
        if since_id is None:
            since_id = self.store.max_id() if not unread_only else None
        deadline = time.time() + timeout
        while True:
            rows = self.store.list(
                since_id=since_id, unread_only=unread_only and since_id is None, limit=50
            )
            if rows:
                items = [row_to_dict(r) for r in rows]
                if request.query.get("mark_read") in ("1", "true", "yes"):
                    self.store.mark_read([int(i["id"]) for i in items])
                return web.json_response({"messages": items, "count": len(items), "timeout": False})
            remain = deadline - time.time()
            if remain <= 0:
                return web.json_response({"messages": [], "count": 0, "timeout": True})
            try:
                await asyncio.wait_for(self.new_message.wait(), min(remain, 1.0))
            except asyncio.TimeoutError:
                pass

    async def h_send(self, request: web.Request) -> web.Response:
        data = await _json_body(request)
        text = data.get("text") or ""
        if not text.strip():
            return web.json_response({"error": "text 不能为空"}, status=400)
        if data.get("dry_run"):
            last = self.store.last_chat() or {}
            return web.json_response(
                {
                    "dry_run": True,
                    "chat_id": data.get("chat_id") or last.get("chatid"),
                    "text": text,
                }
            )
        try:
            res = await self.do_send(
                text,
                chat_id=data.get("chat_id"),
                chattype=data.get("chattype"),
                markdown=bool(data.get("markdown", True)),
            )
        except Exception as e:
            return web.json_response({"error": str(e), "errcode": getattr(e, "errcode", None)}, status=502)
        return web.json_response({"ok": True, **res})

    async def h_reply(self, request: web.Request) -> web.Response:
        data = await _json_body(request)
        ident = str(data.get("msg_id") or data.get("id") or "")
        text = data.get("text") or ""
        if not ident:
            return web.json_response({"error": "缺少 msg_id"}, status=400)
        if not text.strip():
            return web.json_response({"error": "text 不能为空"}, status=400)
        if data.get("dry_run"):
            return web.json_response({"dry_run": True, "msg_id": ident, "text": text})
        try:
            res = await self.do_reply(ident, text)
        except KeyError as e:
            return web.json_response({"error": str(e)}, status=404)
        except Exception as e:
            return web.json_response({"error": str(e), "errcode": getattr(e, "errcode", None)}, status=502)
        return web.json_response({"ok": True, **res})

    async def h_shutdown(self, request: web.Request) -> web.Response:
        self._stop_event.set()
        return web.json_response({"ok": True})

    # ---------- 运行 ----------

    async def run(self) -> None:
        self.client.start()
        app = self.build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg.http_host, self.cfg.http_port)
        await site.start()
        if self.automation:
            self.automation.start()
        log.info(
            "daemon 启动：bot_id=%s http=http://%s:%d 收件箱=%s",
            self.cfg.bot_id,
            self.cfg.http_host,
            self.cfg.http_port,
            paths.inbox_path(),
        )

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                pass

        try:
            await self._stop_event.wait()
        finally:
            log.info("daemon 收到退出信号，正在关闭")
            pending = list(self._attachment_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self.automation:
                await self.automation.close()
            await self.client.stop()
            if self._runner:
                await self._runner.cleanup()
            self.store.close()


def _int_or_none(v: Optional[str]) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _float_or_none(v: Optional[str]) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)  # epoch 秒
    except ValueError:
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


async def _json_body(request: web.Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="wecom-daemon", description="企业微信智能机器人长连接常驻进程")
    ap.add_argument("--log-level", default=None)
    args = ap.parse_args(argv)

    paths.ensure_dirs()
    cfg = load_config()
    setup_logging(args.log_level or cfg.log_level, secrets=[cfg.secret, cfg.http_token], to_stderr=True)

    lock = ProcessLock(paths.lock_path())
    lock.acquire()
    try:
        paths.pid_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
        d = Daemon(cfg)
        asyncio.run(d.run())
    finally:
        lock.release()
        try:
            paths.pid_path().unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
