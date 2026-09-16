"""日志初始化：轮转文件 + 密钥脱敏。"""

from __future__ import annotations

import logging
import logging.handlers
from typing import Iterable, List

from . import paths


class RedactFilter(logging.Filter):
    """把已知敏感串（secret / token）替换成 ***，防止意外落盘。"""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets: List[str] = [s for s in secrets if s and len(s) >= 6]

    def add(self, secret: str) -> None:
        if secret and len(secret) >= 6 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for s in self._secrets:
            if s in redacted:
                redacted = redacted.replace(s, "***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


_redact = RedactFilter()


def redact_filter() -> RedactFilter:
    return _redact


def setup(level: str = "INFO", secrets: Iterable[str] = (), to_stderr: bool = False) -> logging.Logger:
    """配置根 logger：写 ~/Library/Logs/wecom-bot/daemon.log，5MB×5 轮转。"""
    for s in secrets:
        _redact.add(s)

    paths.log_dir().mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    fh = logging.handlers.RotatingFileHandler(
        paths.daemon_log_path(), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.addFilter(_redact)
    root.addHandler(fh)

    if to_stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(_redact)
        root.addHandler(sh)

    # 第三方库降噪
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return root
