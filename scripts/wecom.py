#!/usr/bin/env python3
"""wecom CLI 入口（可直接用仓库内 .venv 的 python 执行）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wecom_bot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
