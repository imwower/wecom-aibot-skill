"""统一管理配置 / 状态 / 日志路径。

所有路径都可以用环境变量覆盖，方便测试与多实例：
  WECOM_BOT_CONFIG    配置文件路径（默认 ~/.config/wecom-bot/config.json）
  WECOM_BOT_STATE_DIR 状态目录（默认 ~/.local/state/wecom-bot）
  WECOM_BOT_LOG_DIR   日志目录（默认 ~/Library/Logs/wecom-bot）
"""

from __future__ import annotations

import os
from pathlib import Path


def config_path() -> Path:
    env = os.environ.get("WECOM_BOT_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "wecom-bot" / "config.json"


def state_dir() -> Path:
    env = os.environ.get("WECOM_BOT_STATE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / "wecom-bot"


def log_dir() -> Path:
    env = os.environ.get("WECOM_BOT_LOG_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Logs" / "wecom-bot"


def inbox_path() -> Path:
    return state_dir() / "inbox.sqlite"


def media_dir() -> Path:
    return state_dir() / "media"


def lock_path() -> Path:
    return state_dir() / "daemon.lock"


def pid_path() -> Path:
    return state_dir() / "daemon.pid"


def daemon_log_path() -> Path:
    return log_dir() / "daemon.log"


def ensure_dirs() -> None:
    for d in (state_dir(), media_dir(), log_dir(), config_path().parent):
        d.mkdir(parents=True, exist_ok=True)
