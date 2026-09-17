"""配置读写。

配置文件默认在 ~/.config/wecom-bot/config.json，权限 600，绝不进仓库。
secret 允许用环境变量 WECOM_BOT_SECRET 覆盖（优先级最高）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict

from . import paths

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
DEFAULT_HTTP_PORT = 47632
DEFAULT_ACK_TEXT = "收到，处理中…"


@dataclass
class Config:
    bot_id: str = ""
    secret: str = ""
    ws_url: str = DEFAULT_WS_URL
    http_host: str = "127.0.0.1"
    http_port: int = DEFAULT_HTTP_PORT
    # 收到消息后 5 秒内回的短确认；置空字符串表示不确认（同时也不会开流，
    # 后续 reply 只能走 aibot_send_msg 主动推送）
    ack_text: str = DEFAULT_ACK_TEXT
    heartbeat_interval: float = 30.0
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 30.0
    # 流式消息官方要求 10 分钟内 finish，留 30 秒安全余量
    stream_window: float = 570.0
    ack_timeout: float = 5.0
    download_media: bool = True
    # 可选：本地 HTTP 接口的简单令牌，设置后请求需带 X-Wecom-Token
    http_token: str = ""
    log_level: str = "INFO"
    # 自动任务：未配置 owner 时一律不执行。默认关闭，兼容手动收发。
    auto_enabled: bool = False
    owner_userid: str = ""
    trigger_prefix: str = "@我的AI"
    private_requires_prefix: bool = False
    ai_provider: str = "codex"  # codex | claude
    ai_executable: str = ""    # 空值时从 PATH 查找对应 CLI
    ai_model: str = ""         # 空值沿用 CLI 自身模型配置
    ai_full_access: bool = False
    ai_cwd: str = "~/code"
    ai_timeout: float = 900.0
    ai_idle_timeout: float = 300.0
    ai_message_window: float = 3.0  # 相邻附件和文字回调的合并等待秒数
    ai_prompt_file: str = ""  # 空值使用内置提示词；可指定自己的 Markdown 文件

    def with_env_overrides(self) -> "Config":
        """应用环境变量覆盖（只覆盖敏感/易变项）。"""
        data = asdict(self)
        if os.environ.get("WECOM_BOT_SECRET"):
            data["secret"] = os.environ["WECOM_BOT_SECRET"]
        if os.environ.get("WECOM_BOT_ID"):
            data["bot_id"] = os.environ["WECOM_BOT_ID"]
        if os.environ.get("WECOM_BOT_WS_URL"):
            data["ws_url"] = os.environ["WECOM_BOT_WS_URL"]
        if os.environ.get("WECOM_BOT_HTTP_PORT"):
            data["http_port"] = int(os.environ["WECOM_BOT_HTTP_PORT"])
        return Config(**data)

    def redacted(self) -> Dict[str, Any]:
        data = asdict(self)
        data["secret"] = "***" if self.secret else ""
        data["http_token"] = "***" if self.http_token else ""
        return data


class ConfigError(RuntimeError):
    pass


def load(required: bool = True) -> Config:
    """读取配置；required=True 时缺少 bot_id/secret 会抛错。"""
    p = paths.config_path()
    data: Dict[str, Any] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"配置文件不是合法 JSON：{p} ({e})") from e
    known = {f for f in Config.__dataclass_fields__}
    unknown = set(data) - known
    for k in unknown:
        data.pop(k)
    cfg = Config(**data).with_env_overrides()
    if cfg.ai_provider not in ('codex', 'claude'):
        raise ConfigError('ai_provider 必须是 codex 或 claude')
    if not isinstance(cfg.ai_full_access, bool):
        raise ConfigError('ai_full_access 必须是布尔值')
    if cfg.ai_timeout <= 0 or cfg.ai_idle_timeout <= 0:
        raise ConfigError('AI 超时必须大于 0')
    if not 0 <= cfg.ai_message_window <= 30:
        raise ConfigError('ai_message_window 必须在 0 到 30 秒之间')
    if required:
        if not cfg.bot_id:
            raise ConfigError(
                f"缺少 bot_id。请先执行：wecom setup --bot-id <ID> --secret <SECRET>（配置文件 {p}）"
            )
        if not cfg.secret:
            raise ConfigError(
                "缺少 secret。请执行 wecom setup --secret <SECRET>，或设置环境变量 WECOM_BOT_SECRET"
            )
    return cfg


def save(cfg: Config) -> Path:
    """写配置文件并 chmod 600。环境变量注入的值不会被写回。"""
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(cfg)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    os.chmod(p, 0o600)
    return p
