"""``core/voice/listen``（被动收听）的行为测试：宿主转写优先 / 兜底判定 / 去重账本。

宿主契约（4.28.1 ``preprocess_stage``）：STT 成功 → Record 被换成 Plain；
链里剩 Record = 宿主没转——这正是兜底的判定依据。测试用同形假对象（鸭子类型）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
sys.path.insert(0, str(_P(__file__).resolve().parent))

from core.voice.listen import (  # noqa: E402
    ListenState,
    fallback_decision,
    file_uri_to_path,
    is_record,
    pending_records,
    record_ref,
)


class _FakeRecord:
    """宿主 Record 的忠实替身（str 枚举 type + file/url/path 字段）。"""

    def __init__(self, file: str = "", url: str = "", path: str = "") -> None:
        self.type = "Record"  # ComponentType 是 str Enum，``== "Record"`` 成立
        self.file = file
        self.url = url
        self.path = path


class _FakePlain:
    def __init__(self, text: str) -> None:
        self.type = "Plain"
        self.text = text


class IsRecordTest(unittest.TestCase):
    def test_string_enum_and_class_name(self) -> None:
        self.assertTrue(is_record(_FakeRecord()))
        self.assertFalse(is_record(_FakePlain("早")))
        self.assertFalse(is_record({"type": "record"}), "dict 是出站 tool_args 形状，不是链组件")
        self.assertFalse(is_record(None))
        self.assertFalse(is_record("Record"))

    def test_class_name_fallback(self) -> None:
        class Record:  # 旧宿主没有 str 枚举时的形状
            pass

        self.assertTrue(is_record(Record()))


class FileUriTest(unittest.TestCase):
    def test_posix_and_windows(self) -> None:
        self.assertEqual(file_uri_to_path("file:///home/u/a.wav"), "/home/u/a.wav")
        self.assertEqual(file_uri_to_path("file:///C:/Users/u/a.wav"), "C:/Users/u/a.wav")

    def test_percent_decoding(self) -> None:
        self.assertEqual(file_uri_to_path("file:///home/u/a%20b.wav"), "/home/u/a b.wav")

    def test_non_file_returns_none(self) -> None:
        self.assertIsNone(file_uri_to_path("http://x/a.wav"))
        self.assertIsNone(file_uri_to_path(""))
        self.assertIsNone(file_uri_to_path("file://"))


class RecordRefTest(unittest.TestCase):
    def test_path_priority_then_file_uri_then_url(self) -> None:
        self.assertEqual(record_ref(_FakeRecord(path="/tmp/a.wav")), "/tmp/a.wav")
        self.assertEqual(record_ref(_FakeRecord(file="file:///tmp/a.wav")), "/tmp/a.wav")
        self.assertEqual(record_ref(_FakeRecord(url="http://x/a.wav")), "http://x/a.wav")
        self.assertIsNone(record_ref(_FakeRecord()), "全空 = 没有指纹")

    def test_bare_file_fallback(self) -> None:
        self.assertEqual(record_ref(_FakeRecord(file="0d2b.amr")), "0d2b.amr",
                         "NapCat 裸文件名也算指纹")

    def test_url_precedes_ambiguous_bare_file(self) -> None:
        self.assertEqual(
            record_ref(_FakeRecord(file="record.amr", url="https://host/audio/unique-token")),
            "https://host/audio/unique-token",
            "裸 file 标识不可靠时使用宿主给出的 url 指纹",
        )


class PendingRecordsTest(unittest.TestCase):
    def test_only_records_and_capped(self) -> None:
        chain = [
            _FakePlain("已转写的文字"),
            _FakeRecord(path="/tmp/a.wav"),
            _FakePlain("中间一句话"),
            _FakeRecord(file="file:///tmp/b.wav"),
        ]
        self.assertEqual(pending_records(chain), [(1, "/tmp/a.wav"), (3, "/tmp/b.wav")])

    def test_empty_chain_and_bad_input(self) -> None:
        self.assertEqual(pending_records([]), [])
        self.assertEqual(pending_records(None), [])
        self.assertEqual(pending_records("不是链"), [])

    def test_record_without_ref_gets_index_fallback(self) -> None:
        self.assertEqual(pending_records([_FakeRecord()]), [(0, "record#0")])


class ListenStateTest(unittest.TestCase):
    def test_first_time_true_then_false(self) -> None:
        state = ListenState()
        self.assertTrue(state.first_time("ev1|/tmp/a.wav"))
        self.assertFalse(state.first_time("ev1|/tmp/a.wav"), "同一 (事件, 音频) 只记账一次")
        self.assertTrue(state.first_time("ev2|/tmp/a.wav"), "另一个事件是新的")

    def test_ttl_expiry(self) -> None:
        clock = [1000.0]
        state = ListenState(ttl_seconds=10.0, clock=lambda: clock[0])
        self.assertTrue(state.first_time("k"))
        clock[0] += 11.0
        self.assertTrue(state.first_time("k"), "过期后视为第一次（最坏=多转一次，不重复回复）")

    def test_lru_capacity(self) -> None:
        clock = [100.0]
        state = ListenState(max_keys=2, ttl_seconds=1e9, clock=lambda: clock[0])
        self.assertTrue(state.first_time("a"))
        self.assertTrue(state.first_time("b"))
        self.assertTrue(state.first_time("c"))  # 容量满：最老的 a 被淘汰
        self.assertTrue(state.first_time("a"))
        self.assertFalse(state.first_time("c"), "刚记的 c 还在")


class FallbackDecisionTest(unittest.TestCase):
    RECORDS = [(0, "/tmp/a.wav")]

    def test_should_transcribe_when_host_off_and_provider_ready(self) -> None:
        reasons = fallback_decision(
            records=self.RECORDS, host_stt_enabled=False,
            provider_available=True, plugin_fallback=True, dedup_ok=[True],
        )
        self.assertEqual(reasons, [""])

    def test_skip_when_plugin_fallback_off(self) -> None:
        reasons = fallback_decision(
            records=self.RECORDS, host_stt_enabled=False,
            provider_available=True, plugin_fallback=False, dedup_ok=[True],
        )
        self.assertIn("listen_fallback", reasons[0])

    def test_skip_when_no_provider(self) -> None:
        reasons = fallback_decision(
            records=self.RECORDS, host_stt_enabled=False,
            provider_available=False, plugin_fallback=True, dedup_ok=[True],
        )
        self.assertIn("提供方", reasons[0])

    def test_no_second_asr_when_host_owns_stt(self) -> None:
        """宿主已开转写却还剩 Record（宿主侧失败）：插件不抢第二次。"""
        reasons = fallback_decision(
            records=self.RECORDS, host_stt_enabled=True,
            provider_available=True, plugin_fallback=True, dedup_ok=[True],
        )
        self.assertIn("不再重复转", reasons[0])

    def test_skip_when_dedup_says_seen(self) -> None:
        reasons = fallback_decision(
            records=self.RECORDS, host_stt_enabled=False,
            provider_available=True, plugin_fallback=True, dedup_ok=[False],
        )
        self.assertIn("去重", reasons[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
