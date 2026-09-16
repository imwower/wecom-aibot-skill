"""wecom 命令行：给人和 agent 用的薄客户端，所有动作都经由本地 daemon。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, paths
from .config import Config, ConfigError, load as load_config, save as save_config
from .lockfile import read_pid


class CliError(RuntimeError):
    pass


# ---------------- HTTP 客户端 ----------------


def _base_url(cfg: Config) -> str:
    return f"http://{cfg.http_host}:{cfg.http_port}"


def _request(
    cfg: Config, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None, timeout: float = 30.0,
) -> Dict[str, Any]:
    url = _base_url(cfg) + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cfg.http_token:
        req.add_header("X-Wecom-Token", cfg.http_token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {"error": f"HTTP {e.code}"}
        raise CliError(payload.get("error") or f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise CliError(
            f"连不上 daemon（{_base_url(cfg)}）：{e.reason}\n"
            f"请先启动：wecom daemon start   （日志：{paths.daemon_log_path()}）"
        ) from e


# ---------------- 输出 ----------------


def _out(args: argparse.Namespace, payload: Any, text: str = "") -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif text:
        print(text)
    else:
        print(json.dumps(payload, ensure_ascii=False))


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M:%S")


def _fmt_messages(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "（没有消息）"
    lines = []
    for m in items:
        flag = " " if m.get("is_read") else "*"
        who = m.get("from_userid") or "-"
        body = (m.get("text") or "").replace("\n", " ")
        extra = f"  media={m['media_path']}" if m.get("media_path") else ""
        lines.append(
            f"{flag}[{m['id']}] {_fmt_ts(m.get('received_at'))} {m.get('msgtype')} {who}: {body}{extra}"
        )
    return "\n".join(lines)


# ---------------- 子命令 ----------------


def cmd_setup(args: argparse.Namespace) -> int:
    paths.ensure_dirs()
    try:
        cfg = load_config(required=False)
    except ConfigError:
        cfg = Config()

    if args.from_env_file:
        p = Path(args.from_env_file).expanduser()
        if not p.exists():
            raise CliError(f"env 文件不存在：{p}")
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().lstrip("export").strip()
            v = v.strip().strip('"').strip("'")
            if k in ("WECOM_BOT_ID", "WECHAT_BOT_ID") and v:
                cfg.bot_id = v
            elif k in ("WECOM_BOT_SECRET", "WECHAT_BOT_SECRET") and v:
                cfg.secret = v

    if args.bot_id:
        cfg.bot_id = args.bot_id
    if args.secret:
        cfg.secret = args.secret
    if args.secret_env:
        v = os.environ.get(args.secret_env)
        if not v:
            raise CliError(f"环境变量 {args.secret_env} 为空")
        cfg.secret = v
    if args.ack_text is not None:
        cfg.ack_text = args.ack_text
    if args.http_port:
        cfg.http_port = args.http_port
    if args.ws_url:
        cfg.ws_url = args.ws_url
    if args.no_media:
        cfg.download_media = False
    if args.log_level:
        cfg.log_level = args.log_level

    p = save_config(cfg)
    payload = {"config": str(p), "mode": oct(p.stat().st_mode & 0o777), **cfg.redacted()}
    _out(args, payload, f"已写入 {p}（600）\nbot_id={cfg.bot_id} secret={'已设置' if cfg.secret else '缺失'} "
                        f"http_port={cfg.http_port} ack_text={cfg.ack_text!r}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(required=False)
    pid = read_pid(paths.lock_path())
    try:
        health = _request(cfg, "GET", "/health", timeout=10)
    except CliError as e:
        payload = {"running": False, "pid": pid, "error": str(e)}
        _out(args, payload, f"daemon 未运行（锁文件 pid={pid or '-'}）\n{e}")
        return 3
    ws = health.get("ws", {})
    text = (
        f"daemon 运行中 pid={health.get('pid')} 已跑 {health.get('uptime')}s\n"
        f"长连接 connected={ws.get('connected')} authenticated={ws.get('authenticated')} "
        f"第 {ws.get('conn_gen')} 代 重连 {ws.get('reconnect_attempts')} 次\n"
        f"bot_id={health.get('bot_id')}  未读 {health.get('unread')}  最近一分钟已发 {health.get('sent_last_minute')} 条\n"
        f"最近会话 {json.dumps(health.get('last_chat'), ensure_ascii=False)}\n"
        f"收件箱 {health.get('inbox')}\n日志 {health.get('log')}"
    )
    if ws.get("last_error"):
        text += f"\n最近错误 {ws['last_error']}"
    _out(args, {"running": True, **health}, text)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    cfg = load_config(required=False)
    res = _request(
        cfg, "GET", "/messages",
        params={
            "unread": 1 if args.unread else None,
            "since": args.since,
            "since_id": args.since_id,
            "limit": args.limit,
            "kind": args.kind,
            "mark_read": 1 if args.mark_read else None,
            "raw": 1 if args.raw else None,
        },
    )
    _out(args, res, _fmt_messages(res.get("messages", [])))
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    cfg = load_config(required=False)
    res = _request(
        cfg, "GET", "/wait",
        params={
            "timeout": args.timeout,
            "since_id": args.since_id,
            "unread": 0 if args.new_only else 1,
            "mark_read": 1 if args.mark_read else None,
        },
        timeout=args.timeout + 15,
    )
    if res.get("timeout"):
        _out(args, res, f"等待 {args.timeout}s 没有新消息")
        return 4
    _out(args, res, _fmt_messages(res.get("messages", [])))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    cfg = load_config(required=False)
    text = _read_text(args)
    res = _request(
        cfg, "POST", "/send",
        body={"text": text, "chat_id": args.to, "markdown": args.markdown, "dry_run": args.dry_run},
    )
    if res.get("dry_run"):
        _out(args, res, f"[dry-run] 目标 {res.get('chat_id')}\n---\n{text}\n---")
        return 0
    _out(args, res, f"已发送（{res.get('route')}）→ {res.get('chat_id')}")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    cfg = load_config(required=False)
    text = _read_text(args)
    res = _request(
        cfg, "POST", "/reply",
        body={"msg_id": args.to, "text": text, "dry_run": args.dry_run},
    )
    if res.get("dry_run"):
        _out(args, res, f"[dry-run] 回复 {args.to}\n---\n{text}\n---")
        return 0
    route = res.get("route")
    note = "（续写流式回复）" if route == "stream" else "（流已失效，改为主动推送）"
    _out(args, res, f"已回复 {args.to} {note}")
    return 0


def cmd_mark_read(args: argparse.Namespace) -> int:
    cfg = load_config(required=False)
    res = _request(cfg, "POST", f"/messages/{args.id}/read")
    _out(args, res, f"已标记已读：{args.id}")
    return 0


def _read_text(args: argparse.Namespace) -> str:
    if getattr(args, "text_file", None):
        p = Path(args.text_file).expanduser()
        if str(p) == "-":
            return sys.stdin.read()
        return p.read_text(encoding="utf-8")
    if not args.text:
        raise CliError("缺少 --text（或 --text-file）")
    return args.text


# ---------------- daemon 管理 ----------------


def _daemon_cmd() -> List[str]:
    return [sys.executable, "-m", "wecom_bot.daemon"]


def cmd_daemon(args: argparse.Namespace) -> int:
    action = args.action
    cfg = load_config(required=(action in ("start", "restart")))
    if action == "start":
        return _daemon_start(args, cfg)
    if action == "stop":
        return _daemon_stop(args, cfg)
    if action == "restart":
        _daemon_stop(args, cfg)
        time.sleep(1.0)
        return _daemon_start(args, cfg)
    if action == "logs":
        return _daemon_logs(args)
    raise CliError(f"未知动作 {action}")


def _daemon_start(args: argparse.Namespace, cfg: Config) -> int:
    paths.ensure_dirs()
    pid = read_pid(paths.lock_path())
    if pid:
        _out(args, {"ok": True, "already_running": True, "pid": pid}, f"daemon 已在运行 pid={pid}")
        return 0
    if args.foreground:
        os.execv(sys.executable, _daemon_cmd())
    out = paths.log_dir() / "daemon.out"
    fh = open(out, "ab")
    env = dict(os.environ)
    repo_src = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = repo_src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen(
        _daemon_cmd(), stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env, cwd=str(Path.home()),
    )
    deadline = time.time() + 20
    last_err = ""
    while time.time() < deadline:
        time.sleep(0.4)
        if proc.poll() is not None:
            tail = _tail(out, 20)
            raise CliError(f"daemon 启动即退出（exit={proc.returncode}）。最后日志：\n{tail}")
        try:
            health = _request(cfg, "GET", "/health", timeout=3)
        except CliError as e:
            last_err = str(e)
            continue
        ws = health.get("ws", {})
        _out(
            args, {"ok": True, "pid": health.get("pid"), **health},
            f"daemon 已启动 pid={health.get('pid')}，HTTP {_base_url(cfg)}\n"
            f"长连接 connected={ws.get('connected')} authenticated={ws.get('authenticated')}\n"
            f"日志 {paths.daemon_log_path()}",
        )
        return 0
    raise CliError(f"daemon 起来了但 HTTP 没就绪：{last_err}\n看日志：wecom daemon logs")


def _daemon_stop(args: argparse.Namespace, cfg: Config) -> int:
    pid = read_pid(paths.lock_path())
    if not pid:
        _out(args, {"ok": True, "running": False}, "daemon 未运行")
        return 0
    try:
        _request(cfg, "POST", "/shutdown", timeout=5)
    except CliError:
        if pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for _ in range(50):
        time.sleep(0.2)
        if not read_pid(paths.lock_path()):
            _out(args, {"ok": True, "stopped": True, "pid": pid}, f"daemon 已停止 pid={pid}")
            return 0
    if pid > 0:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _out(args, {"ok": True, "stopped": True, "killed": True, "pid": pid}, f"daemon 已强制停止 pid={pid}")
    return 0


def _daemon_logs(args: argparse.Namespace) -> int:
    p = paths.daemon_log_path()
    if not p.exists():
        raise CliError(f"还没有日志：{p}")
    if args.follow:
        os.execvp("tail", ["tail", "-f", "-n", str(args.lines), str(p)])
    print(_tail(p, args.lines), end="")
    return 0


def _tail(p: Path, n: int) -> str:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-n:]) + "\n"


# ---------------- 入口 ----------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wecom", description="企业微信智能机器人（长连接）收发工具")
    ap.add_argument("--version", action="version", version=f"wecom {__version__}")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="写入 ~/.config/wecom-bot/config.json")
    p.add_argument("--bot-id")
    p.add_argument("--secret", help="不建议直接写在命令行，优先用 --secret-env / --from-env-file")
    p.add_argument("--secret-env", help="从指定环境变量读取 secret")
    p.add_argument("--from-env-file", help="从 KEY=VALUE 形式的 env 文件读取 WECOM_BOT_ID / WECOM_BOT_SECRET")
    p.add_argument("--ack-text", help="收到消息后 5 秒内回的短确认；传空字符串表示不确认")
    p.add_argument("--http-port", type=int)
    p.add_argument("--ws-url")
    p.add_argument("--no-media", action="store_true", help="不自动下载图片/文件")
    p.add_argument("--log-level")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("status", help="看 daemon 与长连接状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("read", help="读收件箱")
    p.add_argument("--unread", action="store_true", help="只看未读")
    p.add_argument("--since", help="ISO 时间或 epoch 秒")
    p.add_argument("--since-id", type=int, help="只看 id 大于它的")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--kind", choices=["message", "event"])
    p.add_argument("--mark-read", action="store_true")
    p.add_argument("--raw", action="store_true", help="附带原始回调 body")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("wait", help="阻塞等待新消息（agent 等用户回复用）")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--since-id", type=int)
    p.add_argument("--new-only", action="store_true",
                   help="只等调用之后新到的消息；默认会先把已有的未读直接返回")
    p.add_argument("--mark-read", action="store_true")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("send", help="主动推送消息（需要用户先与机器人互动过）")
    p.add_argument("--text")
    p.add_argument("--text-file", help="从文件读正文，- 表示 stdin")
    p.add_argument("--markdown", action="store_true", default=True)
    p.add_argument("--to", help="chat_id；缺省用最近互动的会话")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("reply", help="回复某条收到的消息")
    p.add_argument("--to", required=True, help="收件箱 id 或 msgid")
    p.add_argument("--text")
    p.add_argument("--text-file")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("mark-read", help="标记某条已读")
    p.add_argument("id")
    p.set_defaults(func=cmd_mark_read)

    p = sub.add_parser("daemon", help="管理常驻进程")
    p.add_argument("action", choices=["start", "stop", "restart", "logs"])
    p.add_argument("--foreground", action="store_true", help="前台运行（调试用）")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_daemon)

    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (CliError, ConfigError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
