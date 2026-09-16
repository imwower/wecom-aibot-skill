from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_gateway import FakeGateway  # noqa: E402


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    """把配置 / 状态 / 日志都指到临时目录，绝不碰用户真实数据。"""
    cfg = tmp_path / "config.json"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    monkeypatch.setenv("WECOM_BOT_CONFIG", str(cfg))
    monkeypatch.setenv("WECOM_BOT_STATE_DIR", str(state))
    monkeypatch.setenv("WECOM_BOT_LOG_DIR", str(logs))
    monkeypatch.delenv("WECOM_BOT_SECRET", raising=False)
    monkeypatch.delenv("WECOM_BOT_ID", raising=False)
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return {"config": cfg, "state": state, "logs": logs, "root": tmp_path}


@pytest.fixture
async def gateway():
    gw = FakeGateway()
    await gw.start()
    try:
        yield gw
    finally:
        await gw.stop()
