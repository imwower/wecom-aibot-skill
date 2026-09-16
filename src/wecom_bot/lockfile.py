"""文件锁：保证本机同时只跑一个 daemon。

企微限制同一个 bot 只能有一条长连接，新连接会踢掉旧的；两个 daemon
会互相踢连接形成雪崩，所以必须加锁。
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: Optional[int]) -> None:
        self.pid = pid
        super().__init__(
            f"daemon 已经在运行（pid={pid or '未知'}）。先执行 `wecom daemon stop` 再启动。"
        )


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                pid = int(os.read(fd, 32).decode().strip() or 0) or None
            except Exception:
                pid = None
            os.close(fd)
            raise AlreadyRunning(pid)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def read_pid(path: Path) -> Optional[int]:
    """读锁文件里的 pid；返回 None 表示没有活着的 daemon。"""
    try:
        fd = os.open(str(path), os.O_RDWR)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # 锁被占用 = 有人在跑
            try:
                return int(os.read(fd, 32).decode().strip() or 0) or None
            except Exception:
                return -1
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
    finally:
        os.close(fd)
