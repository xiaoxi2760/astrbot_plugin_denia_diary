"""Exercise the real comment handlers, source store, and JSON error contract."""
import asyncio

from test_web_api_diary import DiaryWriteHandlerCase, NOW
from core.diary.comments import CommentStore


class CommentHandlerTest(DiaryWriteHandlerCase):
    def setUp(self):
        super().setUp()
        self.deps.comments = CommentStore(self.layout, self.diary_store.locks, self.diary_store)

    def test_save_list_publish_unowned_and_archive(self):
        sid = self.ids()[1]
        saved = self.ok_of(self.call("diary_comment_save", body={"book": "normal", "seg_id": sid, "text": "我看到了"}))
        self.assertTrue(saved["ok"])
        cid = saved["entry"]["id"]
        listed = self.ok_of(self.call("diary_comments", query={"book": "normal", "seg_id": sid}))
        self.assertEqual(listed["items"][0]["status"], "draft")
        self.assertFalse(listed["items"][0]["publishable"])
        published = self.ok_of(self.call("diary_comment_publish", body={"id": cid}))
        self.assertFalse(published["ok"])
        deleted = self.ok_of(self.call("diary_comment_delete", body={"id": cid}))
        self.assertEqual(deleted["entry"]["status"], "deleted")

    def test_request_shape_is_400(self):
        self.bad_of(self.call("diary_comments"))
        self.bad_of(self.call("diary_comment_save", body=[]))
        self.bad_of(self.call("diary_comment_save", body={"book": "normal", "seg_id": self.ids()[0], "text": {}}))
        self.bad_of(self.call("diary_comment_publish", body={"id": ["x"]}))

    def test_source_changed_and_future_schema_are_readable_business_errors(self):
        result = self.ok_of(self.call("diary_comment_save", body={"book": "normal", "seg_id": "gone", "text": "留言"}))
        self.assertFalse(result["ok"])
        self.deps.comments.path().write_text('{"schema_version":99,"comments":[]}', encoding="utf-8")
        result = self.ok_of(self.call("diary_comments", query={"book": "normal", "seg_id": self.ids()[0]}))
        self.assertFalse(result["ok"])
        self.assertIn("版本", result["error"])
