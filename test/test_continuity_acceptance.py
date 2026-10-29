"""Independent acceptance cases for realistic diary lengths and visibility changes."""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import compose
from support import NOW, TmpDirCase, make_diary, private_session


class ContinuityAcceptanceTest(TmpDirCase):
    def write(self, diary, text, at):
        result = asyncio.run(diary.write_async(private_session(), text=text, now=at))
        self.assertTrue(result.get('ok'), result)

    def test_long_older_entry_does_not_hide_today_latest(self):
        diary = make_diary(self.root)
        self.write(diary, '昨天一起聊了很多。' * 35 + '明天准备面试。', NOW - timedelta(days=1))
        self.write(diary, '面试已经结束，拿到了录用通知。', NOW - timedelta(hours=1))
        text = compose.compose_prompt(diary, private_session(), now=NOW, query_text='面试怎么样')
        self.assertIn('录用通知', text, '长旧日记不能吃光额度，让今天结果消失')
        self.assertLessEqual(len(text), 400)

    def test_topic_late_in_long_body_is_retrievable(self):
        diary = make_diary(self.root)
        self.write(diary, '今天散步看到一朵花。' * 30 + '小猫团子下周一要去复查。', NOW - timedelta(days=20))
        self.write(diary, '今天在家看电影。', NOW - timedelta(hours=1))
        text = compose.compose_prompt(diary, private_session(), now=NOW, query_text='团子复查')
        self.assertIn('团子', text, '命中了正文后半段时，应给出相关原文，而不是无关开头')
        self.assertIn('复查', text)

    def test_revoked_love_access_cannot_leak_through_status_fallback(self):
        writer = make_diary(self.root, {'love_peers': ['10001']})
        self.write(writer, '只有恋爱本里的秘密', NOW - timedelta(days=1))
        reader = make_diary(self.root, {'love_peers': ['99999']})
        text = compose.compose_prompt(reader, private_session(), now=NOW, query_text='秘密')
        self.assertNotIn('秘密', text, '素材和低优先级状态行都要服从相同的可见性')

    def test_topic_window_uses_original_offsets_after_spaces_and_line_breaks(self):
        diary = make_diary(self.root)
        self.write(diary, 'A quiet day.\n\n' * 30 + '小猫团子下周一要复查。', NOW - timedelta(days=20))
        self.write(diary, '今天在家看电影。', NOW - timedelta(hours=1))
        text = compose.compose_prompt(diary, private_session(), now=NOW, query_text='团子复查')
        self.assertIn('团子', text, '正文含空格和换行时，命中窗口坐标仍须对准原文')
