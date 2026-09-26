import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import Database, DomainError, seed_demo


def due(days=3):
    return (date.today() + timedelta(days=days)).isoformat()


class SubtitleQCFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "dan", "role": "timeline"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def make_cue(self, text="seal 海豹在冰面"):
        return self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": text, "expected_revision": 0})

    def register(self, cue=None, **extra):
        payload = {"time_ms": 1200, "body": "术语正确，请确认冻结时间", "assignee": "bob", "due_at": due(), **extra}
        if cue is not None:
            payload["cue_id"] = cue["id"] if isinstance(cue, dict) else cue
        return self.db.add_comment(self.version, "carol", payload, "reviewer")

    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self.make_cue()
        comment = self.register(cue)
        self.assertEqual(comment["status"], "open")
        self.assertEqual(comment["assignee"], "bob")
        # Responsible member replies; reviewer confirms before submission.
        self.db.reply_comment(comment["id"], "bob", {"note": "已调整冻结时间到 2.8s"}, "translator")
        self.db.confirm_comment(comment["id"], "carol", {"note": "确认无误"}, "reviewer")
        self.db.submit(self.version, "bob")
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000, "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})

    def test_rework_status_flow_records_and_transitions(self):
        comment = self.register()
        # Reply needs translator/timeline permission.
        with self.assertRaisesRegex(DomainError, "翻译或时间轴"):
            self.db.reply_comment(comment["id"], "carol", {"note": "我也能改？"}, "reviewer")
        with self.assertRaisesRegex(DomainError, "处理说明不能为空"):
            self.db.reply_comment(comment["id"], "bob", {"note": "  "}, "translator")
        self.db.reply_comment(comment["id"], "dan", {"note": "时间轴已核对"}, "timeline")
        self.assertEqual(self.db.get_comment(comment["id"])["comment"]["status"], "pending")
        # Only pending comments can be confirmed; reviewers only.
        with self.assertRaisesRegex(DomainError, "复核人"):
            self.db.confirm_comment(comment["id"], "bob", {"note": "我自己关"}, "translator")
        # Reviewer sends it back for rework with a mandatory reason.
        with self.assertRaisesRegex(DomainError, "原因"):
            self.db.rework_comment(comment["id"], "carol", {"note": ""}, "reviewer")
        self.db.rework_comment(comment["id"], "carol", {"note": "结尾帧还差两帧"}, "reviewer")
        self.assertEqual(self.db.get_comment(comment["id"])["comment"]["status"], "open")
        # Re-reply then confirm closes it.
        self.db.reply_comment(comment["id"], "bob", {"note": "已补齐两帧"}, "translator")
        closed = self.db.confirm_comment(comment["id"], "carol", {"note": "好"}, "reviewer")
        self.assertEqual(closed["status"], "closed")
        detail = self.db.get_comment(comment["id"])
        kinds = [r["kind"] for r in detail["records"]]
        self.assertEqual(kinds, ["register", "reply", "rework", "reply", "note"])
        transitions = [(t["from_status"], t["to_status"]) for t in detail["transitions"]]
        self.assertEqual(transitions, [(None, "open"), ("open", "pending"), ("pending", "open"), ("open", "pending"), ("pending", "closed")])
        # A closed comment cannot receive replies.
        with self.assertRaisesRegex(DomainError, "未关闭"):
            self.db.reply_comment(comment["id"], "bob", {"note": "再补一句"}, "translator")

    def test_only_reviewer_registers_with_assignee_and_deadline(self):
        with self.assertRaisesRegex(DomainError, "责任人"):
            self.db.add_comment(self.version, "carol", {"time_ms": 1, "body": "x", "due_at": due()}, "reviewer")
        with self.assertRaisesRegex(DomainError, "期限"):
            self.db.add_comment(self.version, "carol", {"time_ms": 1, "body": "x", "assignee": "bob"}, "reviewer")
        with self.assertRaisesRegex(DomainError, "日期"):
            self.register(due_at="2026/01/01")
        with self.assertRaisesRegex(DomainError, "复核人"):
            self.db.add_comment(self.version, "bob", {"time_ms": 1, "body": "译者提意见", "assignee": "bob", "due_at": due()}, "translator")

    def test_closed_comment_reopens_when_cue_changes(self):
        cue = self.make_cue()
        comment = self.register(cue)
        self.db.reply_comment(comment["id"], "bob", {"note": "已修正"}, "translator")
        self.db.confirm_comment(comment["id"], "carol", {"note": ""}, "reviewer")
        self.assertEqual(self.db.get_comment(comment["id"])["comment"]["status"], "closed")
        self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2800, "text": "海豹在冰面", "expected_revision": 1})
        detail = self.db.get_comment(comment["id"])
        self.assertEqual(detail["comment"]["status"], "open")
        self.assertEqual(detail["records"][-1]["kind"], "system")
        self.assertEqual(detail["transitions"][-1]["to_status"], "open")

    def test_submit_blocked_while_comments_open_or_pending(self):
        self.make_cue()
        open_comment = self.register()
        with self.assertRaisesRegex(DomainError, "未关闭或待确认"):
            self.db.submit(self.version, "bob")
        # Pending is also blocking.
        self.db.reply_comment(open_comment["id"], "bob", {"note": "改了"}, "translator")
        with self.assertRaisesRegex(DomainError, "未关闭或待确认"):
            self.db.submit(self.version, "bob")
        self.db.confirm_comment(open_comment["id"], "carol", {"note": ""}, "reviewer")
        # Timeless comment without cue attachment stays closed and does not block.
        other = self.db.add_comment(self.version, "carol", {"time_ms": 5000, "body": "整体节奏可以", "assignee": "dan", "due_at": due()}, "reviewer")
        self.db.reply_comment(other["id"], "dan", {"note": "收到"}, "timeline")
        self.db.confirm_comment(other["id"], "carol", {"note": ""}, "reviewer")
        submitted = self.db.submit(self.version, "bob")
        self.assertEqual(submitted["status"], "review")

    def test_overdue_flag(self):
        past = self.register(due_at=due(-1))
        self.assertTrue(self.db.list_comments(self.version)[0]["overdue"])
        self.db.reply_comment(past["id"], "bob", {"note": "处理"}, "translator")
        self.db.confirm_comment(past["id"], "carol", {"note": ""}, "reviewer")
        self.assertFalse(self.db.list_comments(self.version)[0]["overdue"])


if __name__ == "__main__":
    unittest.main()
