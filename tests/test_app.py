import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


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

    def _cue(self, **overrides):
        payload = {"cue_index": 1, "start_ms": 1000, "end_ms": 3000,
                   "text": "seal 海豹在冰面", "expected_revision": 0}
        payload.update(overrides)
        return self.db.save_cue(self.version, "bob", payload)

    def test_full_revision_rework_lock_delivery_and_overwrite_protection(self):
        cue = self._cue()
        self.assertEqual(cue["version_revision"], 1)
        comment = self.db.add_comment(self.version, "carol",
                                      {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"},
                                      "reviewer")
        cid = comment["id"]

        # 有未处理意见时，提交复核被拒绝。
        with self.assertRaisesRegex(DomainError, "未关闭或待确认"):
            self.db.submit(self.version, "bob")

        # 复核人登记责任人与期限；未登记前责任人不能回复。
        with self.assertRaisesRegex(DomainError, "尚未登记责任人"):
            self.db.reply_comment(self.version, cid, "bob", {"note": "已调整"}, "translator")
        with self.assertRaisesRegex(DomainError, "只有复核人"):
            self.db.triage_comment(self.version, cid, "bob",
                                   {"assignee": "bob", "due_date": "2026-10-01"}, "translator")
        self.db.triage_comment(self.version, cid, "carol",
                               {"assignee": "bob", "due_date": "2026-10-01"}, "reviewer")

        # 责任人必须是翻译或时间轴成员。
        with self.assertRaisesRegex(DomainError, "翻译或时间轴成员"):
            self.db.triage_comment(self.version, cid, "carol",
                                   {"assignee": "carol", "due_date": "2026-10-01"}, "reviewer")

        # 翻译成员回复处理说明 → 待确认；复核人确认后关闭。
        with self.assertRaisesRegex(DomainError, "翻译或时间轴成员"):
            self.db.reply_comment(self.version, cid, "carol", {"note": "我来处理"}, "reviewer")
        reply = self.db.reply_comment(self.version, cid, "bob",
                                      {"note": "冻结时间已确认为 1200ms"}, "translator")
        self.assertEqual(reply["status"], "pending_confirmation")
        with self.assertRaisesRegex(DomainError, "只有复核人"):
            self.db.confirm_comment(self.version, cid, "bob", {}, "translator")
        # 待确认同样阻塞提交。
        with self.assertRaisesRegex(DomainError, "未关闭或待确认"):
            self.db.submit(self.version, "bob")
        self.db.confirm_comment(self.version, cid, "carol", {"note": "确认无误"}, "reviewer")
        listed = {c["id"]: c for c in self.db.list_comments(self.version)}
        self.assertEqual(listed[cid]["status"], "closed")
        self.assertEqual(listed[cid]["replies"][0]["note"], "冻结时间已确认为 1200ms")

        # 意见全部关闭后，复核交付链路才放行。
        self.db.submit(self.version, "bob")
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 2500,
                                                   "text": "海豹", "expected_revision": 2})

    def test_return_and_reopen_on_cue_change(self):
        cue = self._cue()
        cid = self.db.add_comment(self.version, "carol",
                                  {"cue_id": cue["id"], "time_ms": 1200, "body": "时间点偏晚"},
                                  "reviewer")["id"]
        self.db.triage_comment(self.version, cid, "carol",
                               {"assignee": "dan", "due_date": "2026-10-02"}, "reviewer")
        self.db.reply_comment(self.version, cid, "dan", {"note": "改为 1100ms"}, "timeline")
        # 复核人退回 → 重新打开，可再次回复。
        with self.assertRaisesRegex(DomainError, "必须填写原因"):
            self.db.return_comment(self.version, cid, "carol", {"reason": ""}, "reviewer")
        self.db.return_comment(self.version, cid, "carol", {"reason": "请再核对一帧"}, "reviewer")
        self.db.reply_comment(self.version, cid, "dan", {"note": "已核对，改为 1120ms"}, "timeline")
        self.db.confirm_comment(self.version, cid, "carol", {}, "reviewer")
        self.assertEqual(self.db.list_comments(self.version)[0]["status"], "closed")

        # 该字幕再次修改 → 意见自动重新打开；提交再次被拒绝。
        updated = self.db.save_cue(self.version, "bob",
                                   {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000,
                                    "end_ms": 3000, "text": "海豹在冰面", "expected_revision": 1})
        self.assertEqual(updated["reopened_comments"], [cid])
        self.assertEqual(self.db.list_comments(self.version)[0]["status"], "open")
        with self.assertRaisesRegex(DomainError, "未关闭或待确认"):
            self.db.submit(self.version, "bob")

    def test_time_only_comment_not_reopened_by_cue_change(self):
        cue = self._cue()
        self.db.add_comment(self.version, "carol", {"time_ms": 5000, "body": "整体节奏问题"}, "reviewer")
        # 没有挂在具体字幕上的意见不随字幕修改重开；但仍阻塞提交直至闭环。
        self.db.save_cue(self.version, "bob",
                         {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000,
                          "end_ms": 2800, "text": "海豹在冰面", "expected_revision": 1})
        events = self.db.list_comment_events(self.version)
        self.assertFalse(any(e["transition"] == "reopened" for e in events))

    def test_comment_events_recorded_separately(self):
        cue = self._cue()
        cid = self.db.add_comment(self.version, "carol",
                                  {"cue_id": cue["id"], "time_ms": 1200, "body": "需返修"},
                                  "reviewer")["id"]
        self.db.triage_comment(self.version, cid, "carol",
                               {"assignee": "bob", "due_date": "2026-10-01"}, "reviewer")
        self.db.reply_comment(self.version, cid, "bob", {"note": "已修"}, "translator")
        self.db.confirm_comment(self.version, cid, "carol", {}, "reviewer")
        transitions = [e["transition"] for e in self.db.list_comment_events(self.version)]
        self.assertEqual(transitions, ["triaged", "replied", "confirmed"])

    def test_revision_overlap_glossary_and_permissions(self):
        first = self.db.save_cue(self.version, "bob",
                                 {"cue_index": 1, "start_ms": 1000, "end_ms": 3000,
                                  "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1,
                                                   "start_ms": 1000, "end_ms": 2500,
                                                   "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000,
                                                   "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000,
                                                   "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000,
                                                     "text": "海豹", "expected_revision": 1})


if __name__ == "__main__":
    unittest.main()
