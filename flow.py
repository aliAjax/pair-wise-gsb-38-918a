"""评论返修流程：状态机、权限校验与提交复核闸门。

状态流转：
    open（复核人登记责任人与期限）
      → open（翻译/时间轴成员回复处理说明）→ pending_confirmation
      → closed（复核人确认关闭）
    pending_confirmation → open（复核人退回补充处理）
    closed/pending_confirmation → open（关联字幕再次修改，自动重新打开）
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from core import DomainError
from records import CommentStore, STATUS_LABELS

_DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EDIT_ROLES = ("translator", "timeline")


def can_edit_cues(conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
    """获得翻译或时间轴权限的成员（项目负责人亦然）。"""
    if actor == version["owner"]:
        return True
    placeholders = ",".join("?" for _ in _EDIT_ROLES)
    return bool(conn.execute(
        f"SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ({placeholders})",
        (version["id"], actor, *_EDIT_ROLES),
    ).fetchone())


def is_reviewer(conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
    if actor == version["owner"]:
        return True
    return bool(conn.execute(
        "SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'",
        (version["id"], actor),
    ).fetchone())


def _require_reviewer(conn: sqlite3.Connection, version: sqlite3.Row, actor: str, action: str) -> None:
    if not is_reviewer(conn, version, actor):
        raise DomainError(f"只有复核人可以{action}", 403)


def _load_comment(store: CommentStore, version_id: int, comment_id: int) -> sqlite3.Row:
    comment = store.get(comment_id, version_id)
    if not comment:
        raise DomainError("评论不存在", 404)
    return comment


def triage_comment(conn: sqlite3.Connection, version: sqlite3.Row, comment_id: int,
                   actor: str, payload: dict[str, Any]) -> dict[str, Any]:
    """复核人登记责任人和处理期限。"""
    _require_reviewer(conn, version, actor, "登记返修责任人")
    assignee = str(payload.get("assignee", "")).strip()
    due_date = str(payload.get("due_date", "")).strip()
    if not assignee:
        raise DomainError("必须指定返修责任人")
    if not _DUE_RE.match(due_date):
        raise DomainError("处理期限必须是 YYYY-MM-DD 日期")
    placeholders = ",".join("?" for _ in _EDIT_ROLES)
    assigned = conn.execute(
        f"SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ({placeholders})",
        (version["id"], assignee, *_EDIT_ROLES),
    ).fetchone()
    if not assigned and assignee != version["owner"]:
        raise DomainError("责任人必须是该版本的翻译或时间轴成员", 409)
    store = CommentStore(conn)
    comment = _load_comment(store, int(version["id"]), int(comment_id))
    if comment["status"] != "open":
        raise DomainError(f"意见当前为{STATUS_LABELS[comment['status']]}，不能重复登记", 409)
    store.triage(comment, actor, assignee, due_date)
    return {"id": comment["id"], "status": "open", "assignee": assignee, "due_date": due_date}


def reply_comment(conn: sqlite3.Connection, version: sqlite3.Row, comment_id: int,
                  actor: str, payload: dict[str, Any]) -> dict[str, Any]:
    """责任人（翻译/时间轴权限成员）回复并写明处理说明。"""
    if not can_edit_cues(conn, version, actor):
        raise DomainError("只有翻译或时间轴成员可以回复处理说明", 403)
    note = str(payload.get("note", payload.get("body", ""))).strip()
    if not note:
        raise DomainError("处理说明不能为空")
    store = CommentStore(conn)
    comment = _load_comment(store, int(version["id"]), int(comment_id))
    if comment["status"] != "open":
        raise DomainError(f"意见当前为{STATUS_LABELS[comment['status']]}，请等待复核人处理", 409)
    if not comment["assignee"]:
        raise DomainError("复核人尚未登记责任人，不能回复", 409)
    reply_id = store.add_reply(comment, actor, note)
    return {"id": comment["id"], "status": "pending_confirmation", "reply_id": reply_id, "note": note, "user": actor}


def confirm_comment(conn: sqlite3.Connection, version: sqlite3.Row, comment_id: int,
                    actor: str, payload: dict[str, Any]) -> dict[str, Any]:
    """复核人确认后关闭。"""
    _require_reviewer(conn, version, actor, "确认关闭意见")
    store = CommentStore(conn)
    comment = _load_comment(store, int(version["id"]), int(comment_id))
    if comment["status"] != "pending_confirmation":
        raise DomainError("只有待确认的意见可以确认关闭", 409)
    note = str(payload.get("note", "")).strip()
    store.confirm(comment, actor, note)
    return {"id": comment["id"], "status": "closed"}


def return_comment(conn: sqlite3.Connection, version: sqlite3.Row, comment_id: int,
                   actor: str, payload: dict[str, Any]) -> dict[str, Any]:
    """复核人不认可处理说明，退回责任人继续返修。"""
    _require_reviewer(conn, version, actor, "退回意见")
    reason = str(payload.get("reason", payload.get("note", ""))).strip()
    if not reason:
        raise DomainError("退回时必须填写原因")
    store = CommentStore(conn)
    comment = _load_comment(store, int(version["id"]), int(comment_id))
    if comment["status"] != "pending_confirmation":
        raise DomainError("只有待确认的意见可以退回", 409)
    store.send_back(comment, actor, reason)
    return {"id": comment["id"], "status": "open", "reason": reason}


def reopen_for_cue(conn: sqlite3.Connection, version_id: int, cue_id: int, actor: str) -> list[int]:
    """该字幕再次修改时，挂在它上面的已关闭/待确认意见重新打开。"""
    store = CommentStore(conn)
    reopened: list[int] = []
    for comment in store.attached(version_id, cue_id):
        store.reopen(comment, actor, "关联字幕再次修改", cue_id)
        reopened.append(int(comment["id"]))
    return reopened


def assert_submittable(conn: sqlite3.Connection, version_id: int) -> None:
    """提交复核闸门：仍有未关闭或待确认意见时拒绝提交。"""
    blocking = CommentStore(conn).blocking(version_id)
    if blocking:
        summary = "、".join(f"#{c['id']}（{STATUS_LABELS[c['status']]}）" for c in blocking[:5])
        more = "等" if len(blocking) > 5 else ""
        raise DomainError(f"仍有未关闭或待确认的意见：{summary}{more}，提交复核已拒绝", 409)
