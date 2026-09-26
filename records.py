"""评论记录层：评论、处理说明回复、状态事件的存取，不做权限与状态机判断。

- comments：意见主体（责任人 assignee、处理期限 due_date、当前状态）。
- comment_replies：翻译/时间轴成员写下的处理说明，一条意见可有多条。
- comment_events：状态流转流水，每次登记、回复、确认、退回、重新打开都留痕。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from core import utcnow

# 状态：open（待处理）→ pending_confirmation（待确认）→ closed（已关闭）；
# 退回或关联字幕再次修改会回到 open。
STATUSES = ("open", "pending_confirmation", "closed")
STATUS_LABELS = {
    "open": "待处理",
    "pending_confirmation": "待确认",
    "closed": "已关闭",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS comment_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    user TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    transition TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

MIGRATIONS = (
    ("assignee", "ALTER TABLE comments ADD COLUMN assignee TEXT"),
    ("due_date", "ALTER TABLE comments ADD COLUMN due_date TEXT"),
)


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(comments)")}
    for column, ddl in MIGRATIONS:
        if column not in existing:
            conn.execute(ddl)


class CommentStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 写入 ----
    def insert(self, version_id: int, cue_id: int | None, user: str, time_ms: int, body: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO comments(version_id,cue_id,user,time_ms,body,status,created_at) VALUES(?,?,?,?,?, 'open', ?)",
            (version_id, cue_id, user, time_ms, body, utcnow()),
        )
        return int(cur.lastrowid)

    def add_event(self, comment_id: int, version_id: int, actor: str, transition: str,
                  from_status: str | None, to_status: str, detail: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO comment_events(comment_id,version_id,actor,transition,from_status,to_status,detail,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (comment_id, version_id, actor, transition, from_status, to_status,
             json.dumps(detail or {}, ensure_ascii=False), utcnow()),
        )

    def triage(self, comment: sqlite3.Row, actor: str, assignee: str, due_date: str) -> None:
        self.conn.execute("UPDATE comments SET assignee=?, due_date=? WHERE id=?", (assignee, due_date, comment["id"]))
        self.add_event(comment["id"], comment["version_id"], actor, "triaged", comment["status"], comment["status"],
                       {"assignee": assignee, "due_date": due_date})

    def add_reply(self, comment: sqlite3.Row, actor: str, note: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO comment_replies(comment_id,version_id,user,note,created_at) VALUES(?,?,?,?,?)",
            (comment["id"], comment["version_id"], actor, note, utcnow()),
        )
        self.conn.execute("UPDATE comments SET status='pending_confirmation' WHERE id=?", (comment["id"],))
        self.add_event(comment["id"], comment["version_id"], actor, "replied", comment["status"],
                       "pending_confirmation", {"reply_id": int(cur.lastrowid), "note": note})
        return int(cur.lastrowid)

    def confirm(self, comment: sqlite3.Row, actor: str, note: str) -> None:
        self.conn.execute("UPDATE comments SET status='closed' WHERE id=?", (comment["id"],))
        self.add_event(comment["id"], comment["version_id"], actor, "confirmed", comment["status"], "closed",
                       {"note": note})

    def send_back(self, comment: sqlite3.Row, actor: str, reason: str) -> None:
        self.conn.execute("UPDATE comments SET status='open' WHERE id=?", (comment["id"],))
        self.add_event(comment["id"], comment["version_id"], actor, "returned", comment["status"], "open",
                       {"reason": reason})

    def reopen(self, comment: sqlite3.Row, actor: str, reason: str, cue_id: int) -> None:
        self.conn.execute("UPDATE comments SET status='open' WHERE id=?", (comment["id"],))
        self.add_event(comment["id"], comment["version_id"], actor, "reopened", comment["status"], "open",
                       {"reason": reason, "cue_id": cue_id})

    # ---- 读取 ----
    def get(self, comment_id: int, version_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM comments WHERE id=? AND version_id=?", (comment_id, version_id)
        ).fetchone()

    def list(self, version_id: int, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM comments WHERE version_id=? AND status=? ORDER BY id", (version_id, status)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()
        return [dict(r) for r in rows]

    def replies_for(self, version_id: int) -> dict[int, list[dict[str, Any]]]:
        result: dict[int, list[dict[str, Any]]] = {}
        rows = self.conn.execute(
            "SELECT * FROM comment_replies WHERE version_id=? ORDER BY id", (version_id,)
        ).fetchall()
        for row in rows:
            result.setdefault(row["comment_id"], []).append(dict(row))
        return result

    def events_for(self, version_id: int) -> dict[int, list[dict[str, Any]]]:
        result: dict[int, list[dict[str, Any]]] = {}
        rows = self.conn.execute(
            "SELECT * FROM comment_events WHERE version_id=? ORDER BY id", (version_id,)
        ).fetchall()
        for row in rows:
            event = dict(row)
            event["detail"] = json.loads(event["detail"] or "{}")
            result.setdefault(row["comment_id"], []).append(event)
        return result

    def blocking(self, version_id: int) -> list[sqlite3.Row]:
        """未关闭或待确认的意见，提交复核时据此拒绝。"""
        return self.conn.execute(
            "SELECT * FROM comments WHERE version_id=? AND status<> 'closed' ORDER BY id", (version_id,)
        ).fetchall()

    def attached(self, version_id: int, cue_id: int) -> list[sqlite3.Row]:
        """挂在该字幕上、需要随字幕再次修改重新打开的意见。"""
        return self.conn.execute(
            "SELECT * FROM comments WHERE version_id=? AND cue_id=? AND status<> 'open' ORDER BY id",
            (version_id, cue_id),
        ).fetchall()
