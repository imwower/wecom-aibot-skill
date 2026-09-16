"""本地收件箱（SQLite）。

表：
  messages  收到的每条消息 / 事件
  kv        运行期小状态（最近互动会话等）
  sent      发出去的消息流水，便于排查
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid         TEXT UNIQUE,
    req_id        TEXT,
    kind          TEXT NOT NULL DEFAULT 'message',   -- message | event
    aibotid       TEXT,
    chatid        TEXT,
    chattype      TEXT,
    from_userid   TEXT,
    msgtype       TEXT,
    text          TEXT,
    media_path    TEXT,
    raw           TEXT,
    received_at   REAL NOT NULL,
    create_time   INTEGER,
    is_read       INTEGER NOT NULL DEFAULT 0,
    -- 流式回复状态
    stream_id     TEXT,
    stream_opened_at REAL,
    stream_finished  INTEGER NOT NULL DEFAULT 0,
    conn_gen      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(is_read, received_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sent (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at   REAL NOT NULL,
    route     TEXT,          -- stream | send_msg
    chatid    TEXT,
    reply_to  TEXT,          -- 关联的 msgid
    msgtype   TEXT,
    content   TEXT,
    ok        INTEGER,
    errcode   INTEGER,
    errmsg    TEXT
);
"""

COLUMNS = (
    "id, msgid, req_id, kind, aibotid, chatid, chattype, from_userid, msgtype, "
    "text, media_path, raw, received_at, create_time, is_read, stream_id, "
    "stream_opened_at, stream_finished, conn_gen"
)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- 消息 ----------

    def add_message(
        self,
        *,
        msgid: Optional[str],
        req_id: str,
        kind: str,
        body: Dict[str, Any],
        text: str,
        media_path: Optional[str] = None,
        conn_gen: int = 0,
    ) -> Optional[int]:
        """写入一条消息；msgid 重复（企微可能重推）时返回 None。"""
        now = time.time()
        try:
            cur = self._conn.execute(
                "INSERT INTO messages (msgid, req_id, kind, aibotid, chatid, chattype,"
                " from_userid, msgtype, text, media_path, raw, received_at, create_time,"
                " conn_gen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msgid,
                    req_id,
                    kind,
                    body.get("aibotid"),
                    body.get("chatid"),
                    body.get("chattype"),
                    (body.get("from") or {}).get("userid"),
                    body.get("msgtype"),
                    text,
                    media_path,
                    json.dumps(body, ensure_ascii=False),
                    now,
                    body.get("create_time"),
                    conn_gen,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def set_media_path(self, row_id: int, media_path: str) -> None:
        self._conn.execute("UPDATE messages SET media_path=? WHERE id=?", (media_path, row_id))
        self._conn.commit()

    def set_stream(self, row_id: int, stream_id: str, opened_at: float, conn_gen: int) -> None:
        self._conn.execute(
            "UPDATE messages SET stream_id=?, stream_opened_at=?, stream_finished=0, conn_gen=?"
            " WHERE id=?",
            (stream_id, opened_at, conn_gen, row_id),
        )
        self._conn.commit()

    def finish_stream(self, row_id: int) -> None:
        self._conn.execute("UPDATE messages SET stream_finished=1 WHERE id=?", (row_id,))
        self._conn.commit()

    def get(self, ident: str) -> Optional[sqlite3.Row]:
        """按自增 id 或 msgid 取一条。"""
        if ident.isdigit():
            row = self._conn.execute(
                f"SELECT {COLUMNS} FROM messages WHERE id=?", (int(ident),)
            ).fetchone()
            if row:
                return row
        return self._conn.execute(
            f"SELECT {COLUMNS} FROM messages WHERE msgid=?", (ident,)
        ).fetchone()

    def list(
        self,
        *,
        since: Optional[float] = None,
        since_id: Optional[int] = None,
        unread_only: bool = False,
        limit: int = 50,
        kind: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        sql = f"SELECT {COLUMNS} FROM messages WHERE 1=1"
        args: List[Any] = []
        if since is not None:
            sql += " AND received_at > ?"
            args.append(since)
        if since_id is not None:
            sql += " AND id > ?"
            args.append(since_id)
        if unread_only:
            sql += " AND is_read = 0"
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(int(limit))
        return list(self._conn.execute(sql, args))

    def mark_read(self, ids: List[int]) -> int:
        if not ids:
            return 0
        q = ",".join("?" for _ in ids)
        cur = self._conn.execute(f"UPDATE messages SET is_read=1 WHERE id IN ({q})", ids)
        self._conn.commit()
        return cur.rowcount

    def unread_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) c FROM messages WHERE is_read=0").fetchone()
        return int(row["c"])

    def max_id(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(id),0) m FROM messages").fetchone()
        return int(row["m"])

    # ---------- kv ----------

    def set_kv(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self._conn.commit()

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def remember_chat(self, chatid: Optional[str], chattype: Optional[str]) -> None:
        if not chatid:
            return
        self.set_kv("last_chat", {"chatid": chatid, "chattype": chattype, "at": time.time()})

    def last_chat(self) -> Optional[Dict[str, Any]]:
        return self.get_kv("last_chat")

    # ---------- 发送流水 ----------

    def add_sent(
        self,
        *,
        route: str,
        chatid: Optional[str],
        reply_to: Optional[str],
        msgtype: str,
        content: str,
        ok: bool,
        errcode: Optional[int] = None,
        errmsg: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO sent (sent_at, route, chatid, reply_to, msgtype, content, ok, errcode,"
            " errmsg) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), route, chatid, reply_to, msgtype, content[:2000], int(ok), errcode, errmsg),
        )
        self._conn.commit()

    def sent_count_since(self, since: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM sent WHERE sent_at > ? AND ok=1", (since,)
        ).fetchone()
        return int(row["c"])


def row_to_dict(row: sqlite3.Row, include_raw: bool = False) -> Dict[str, Any]:
    d = dict(row)
    if not include_raw:
        d.pop("raw", None)
    else:
        try:
            d["raw"] = json.loads(d["raw"]) if d.get("raw") else None
        except json.JSONDecodeError:
            pass
    d["is_read"] = bool(d.get("is_read"))
    d["stream_finished"] = bool(d.get("stream_finished"))
    return d
