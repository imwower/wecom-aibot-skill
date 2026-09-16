"""CLI 端到端：真起一个 daemon 子进程，连模拟网关，跑 setup/status/read/wait/send/reply。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WECOM = [sys.executable, str(ROOT / "scripts" / "wecom.py")]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Cli:
    def __init__(self, env: dict) -> None:
        self.env = env

    async def run(self, *args: str, timeout: float = 40.0, check: bool = True):
        proc = await asyncio.create_subprocess_exec(
            *WECOM, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        res = (proc.returncode, out.decode(), err.decode())
        if check and proc.returncode != 0:
            raise AssertionError(f"wecom {' '.join(args)} 退出码 {proc.returncode}\n{out.decode()}\n{err.decode()}")
        return res

    async def json(self, *args: str, timeout: float = 40.0, check: bool = True):
        code, out, err = await self.run("--json", *args, timeout=timeout, check=check)
        return code, (json.loads(out) if out.strip() else {}), err


@pytest.fixture
def cli_env(state_env):
    env = dict(os.environ)
    env["WECOM_BOT_CONFIG"] = str(state_env["config"])
    env["WECOM_BOT_STATE_DIR"] = str(state_env["state"])
    env["WECOM_BOT_LOG_DIR"] = str(state_env["logs"])
    env.pop("WECOM_BOT_SECRET", None)
    env.pop("WECOM_BOT_ID", None)
    env["PYTHONUNBUFFERED"] = "1"
    # 用随机端口，避免撞上本机真实在跑的 daemon（默认 47632）
    env["WECOM_BOT_HTTP_PORT"] = str(free_port())
    return env


async def test_cli_full_flow(state_env, gateway, cli_env):
    cli = Cli(cli_env)
    port = int(cli_env["WECOM_BOT_HTTP_PORT"])

    # daemon 没起来时，status 要明确告诉怎么启动
    code, out, err = await cli.run("status", check=False)
    assert code == 3
    assert "wecom daemon start" in (out + err)

    # setup 写配置并 chmod 600
    code, data, _ = await cli.json(
        "setup", "--bot-id", gateway.bot_id, "--secret", gateway.secret,
        "--ws-url", gateway.url, "--http-port", str(port),
    )
    assert data["secret"] == "***"          # 输出里绝不出现明文 secret
    assert data["mode"] == "0o600"
    assert state_env["config"].exists()
    raw = state_env["config"].read_text()
    assert gateway.secret in raw            # 落盘在 600 的配置里
    assert oct(state_env["config"].stat().st_mode & 0o777) == "0o600"

    try:
        # 启动 daemon
        code, data, _ = await cli.json("daemon", "start")
        assert data["ok"] is True
        await asyncio.wait_for(gateway.authed.wait(), 10)

        code, data, _ = await cli.json("status")
        assert data["running"] is True
        assert data["ws"]["authenticated"] is True
        assert data["bot_id"] == gateway.bot_id
        assert gateway.secret not in json.dumps(data)   # status 不泄密

        # 重复 start 不会起第二个（文件锁）
        code, data, _ = await cli.json("daemon", "start")
        assert data.get("already_running") is True

        # 收消息
        pushed = await gateway.push_text("你好，这是测试")
        await asyncio.sleep(1.0)
        code, data, _ = await cli.json("read", "--unread")
        assert data["count"] == 1
        msg = data["messages"][0]
        assert msg["text"] == "你好，这是测试"
        assert msg["is_read"] is False
        msg_id = msg["id"]

        # 网关应当在 5 秒内收到确认帧
        assert gateway.responds and gateway.responds[0]["headers"]["req_id"] == pushed["req_id"]

        # mark-read
        await cli.json("read", "--unread", "--mark-read")
        code, data, _ = await cli.json("read", "--unread")
        assert data["count"] == 0

        # reply 走流式续写
        code, data, _ = await cli.json("reply", "--to", str(msg_id), "--text", "收到啦")
        assert data["route"] == "stream"
        assert gateway.streams[pushed["req_id"]].contents[-1] == "收到啦"
        assert gateway.streams[pushed["req_id"]].finished

        # send --dry-run 不发
        before = len(gateway.sends)
        code, data, _ = await cli.json("send", "--text", "预览一下", "--dry-run")
        assert data["dry_run"] is True
        assert len(gateway.sends) == before

        # send 真发，默认落到最近互动会话
        code, data, _ = await cli.json("send", "--text", "主动推送测试")
        assert data["ok"] is True
        assert gateway.sends[-1]["body"]["markdown"]["content"] == "主动推送测试"
        assert gateway.sends[-1]["body"]["chatid"] == "GeorgeTest"

        # wait 阻塞直到有新消息
        waiter = asyncio.ensure_future(cli.json("wait", "--timeout", "15"))
        await asyncio.sleep(1.0)
        await gateway.push_text("agent 在等的回复")
        code, data, _ = await waiter
        assert data["timeout"] is False
        assert data["messages"][-1]["text"] == "agent 在等的回复"

        # --new-only 时没有新消息 → 超时返回 4
        code, data, _ = await cli.json("wait", "--timeout", "1", "--new-only", check=False)
        assert code == 4 and data["timeout"] is True

        # 不带 --new-only 时，已有未读会立刻返回（agent 不会漏消息）
        code, data, _ = await cli.json("wait", "--timeout", "1")
        assert data["timeout"] is False and data["count"] >= 1

        # 日志里不能出现 secret
        log = (state_env["logs"] / "daemon.log").read_text(errors="replace")
        assert gateway.secret not in log
        assert "鉴权成功" in log
    finally:
        await cli.run("daemon", "stop", check=False)


async def test_cli_setup_from_env_file(state_env, cli_env, tmp_path):
    cli = Cli(cli_env)
    envfile = tmp_path / "credentials.env"
    envfile.write_text("WECOM_BOT_ID=aibFROMFILE\nWECOM_BOT_SECRET=secret-from-file-123\n")
    code, data, _ = await cli.json("setup", "--from-env-file", str(envfile))
    assert data["bot_id"] == "aibFROMFILE"
    assert data["secret"] == "***"
    saved = json.loads(state_env["config"].read_text())
    assert saved["secret"] == "secret-from-file-123"


async def test_cli_send_without_daemon_errors_clearly(state_env, cli_env, gateway):
    cli = Cli(cli_env)
    await cli.json("setup", "--bot-id", gateway.bot_id, "--secret", gateway.secret,
                   "--ws-url", gateway.url, "--http-port", int(cli_env["WECOM_BOT_HTTP_PORT"]) and cli_env["WECOM_BOT_HTTP_PORT"])
    code, out, err = await cli.run("send", "--text", "x", check=False)
    assert code == 2
    assert "wecom daemon start" in (out + err)
