"""``core/voice/status``（能力绑定报告）的行为测试：只写真话——没有的写"不可用"，
不知道的写"未知"，**情绪音色控制永远是"不支持"**（宿主 get_audio 只有文本参）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
sys.path.insert(0, str(_P(__file__).resolve().parent))

from core.voice.status import (  # noqa: E402
    EMOTION_VOICE_REASON,
    VoiceEvents,
    attach_stream_fact,
    report,
)


def _report(**kwargs) -> dict:
    base = {"voice_cfg": {"mode": "voice", "probability": 80}}
    base.update(kwargs)
    return report(**base)


class ReportTest(unittest.TestCase):
    def test_emotion_voice_never_claimed(self) -> None:
        """验收第 5 条：仅接收 text 的 TTS 不能假称支持情绪音色参数。"""
        for tts_probe in (None, {"available": True, "detail": ""}, {"available": False, "detail": "x"}):
            payload = _report(tts_probe=tts_probe)
            self.assertFalse(payload["active"]["emotion_voice"]["supported"], tts_probe)
            self.assertIn("只有文本参数", payload["active"]["emotion_voice"]["reason"])
        self.assertEqual(EMOTION_VOICE_REASON, payload["active"]["emotion_voice"]["reason"])

    def test_tts_unknown_when_not_probed(self) -> None:
        payload = _report()
        self.assertIsNone(payload["active"]["tts"]["available"], "没探测到就写'未知'，不编")
        self.assertIn("未能探测", payload["active"]["tts"]["detail"])

    def test_tts_available_and_unavailable(self) -> None:
        ok = _report(tts_probe={"available": True, "detail": ""})
        self.assertTrue(ok["active"]["tts"]["available"])
        no = _report(tts_probe={"available": False, "detail": "宿主没有已启用的 TTS 提供方"})
        self.assertFalse(no["active"]["tts"]["available"])
        self.assertIn("TTS 提供方", no["active"]["tts"]["detail"])

    def test_listen_host_mode(self) -> None:
        payload = _report(
            stt_probe={"available": True, "detail": ""},
            host_stt_settings={"enable": True},
        )
        self.assertEqual(payload["listen"]["effective"], "host")
        self.assertIn("宿主管线自动转写", payload["listen"]["detail"])
        self.assertTrue(payload["listen"]["host_stt_enabled"])

    def test_listen_plugin_fallback_mode(self) -> None:
        payload = _report(
            stt_probe={"available": True, "detail": ""},
            host_stt_settings={"enable": False},
            voice_cfg={"mode": "off", "listen_fallback": True},
        )
        self.assertEqual(payload["listen"]["effective"], "plugin-fallback")

    def test_listen_none_when_fallback_disabled(self) -> None:
        payload = _report(
            stt_probe={"available": True, "detail": ""},
            host_stt_settings={"enable": False},
            voice_cfg={"mode": "off", "listen_fallback": False},
        )
        self.assertEqual(payload["listen"]["effective"], "none")
        self.assertIn("兜底已关", payload["listen"]["detail"])

    def test_listen_none_when_no_provider(self) -> None:
        payload = _report(
            stt_probe={"available": False, "detail": "宿主没有已配置的语音识别提供方"},
            host_stt_settings={"enable": False},
        )
        self.assertEqual(payload["listen"]["effective"], "none")
        self.assertIn("听不懂", payload["listen"]["detail"])

    def test_file_service_modes(self) -> None:
        off = _report(host_tts_settings={"use_file_service": False})
        self.assertEqual(off["file_service"]["mode"], "local-path")
        self.assertIn("同文件系统", off["file_service"]["detail"])

        no_base = _report(host_tts_settings={"use_file_service": True}, callback_api_base="")
        self.assertEqual(no_base["file_service"]["mode"], "local-path")
        self.assertIn("callback_api_base", no_base["file_service"]["detail"])

        full = _report(host_tts_settings={"use_file_service": True}, callback_api_base="http://h:6185")
        self.assertEqual(full["file_service"]["mode"], "url")
        self.assertIn("限时下载", full["file_service"]["detail"])

    def test_untested_note_always_present(self) -> None:
        payload = _report()
        self.assertFalse(payload["device_verified"])
        self.assertIn("未在手机实机上验证", payload["untested_note"])


class StreamFactTest(unittest.TestCase):
    def test_stream_fact_attached_only_with_provider(self) -> None:
        payload = _report(tts_probe={"available": True, "detail": ""})
        self.assertIsNone(payload["active"]["tts"]["stream"], "没拿到 provider 实例前是未知")

        class _Provider:
            def support_stream(self) -> bool:
                return True

        payload = attach_stream_fact(payload, _Provider())
        self.assertTrue(payload["active"]["tts"]["stream"])

    def test_stream_probe_failure_is_unknown_not_crash(self) -> None:
        class _BadProvider:
            def support_stream(self) -> bool:
                raise RuntimeError("boom")

        payload = attach_stream_fact(_report(), _BadProvider())
        self.assertIsNone(payload["active"]["tts"]["stream"])

    def test_no_support_stream_method_keeps_unknown(self) -> None:
        payload = attach_stream_fact(_report(), object())
        self.assertIsNone(payload["active"]["tts"]["stream"])


class VoiceEventsTest(unittest.TestCase):
    def test_add_and_tail_newest_first(self) -> None:
        events = VoiceEvents()
        events.add("active-tts", ok=True, detail="mode=voice", segments=2)
        events.add("passive-stt", ok=False, detail="#0 转写超时")
        tail = events.tail()
        self.assertEqual(tail[0]["kind"], "passive-stt", "最新在前")
        self.assertEqual(tail[-1]["segments"], 2)
        self.assertFalse(tail[0]["ok"])

    def test_ring_capacity(self) -> None:
        events = VoiceEvents(maxlen=3)
        for i in range(5):
            events.add("active-tts", ok=True, detail=f"e{i}")
        tail = events.tail()
        self.assertEqual(len(tail), 3)
        self.assertEqual(tail[0]["detail"], "e4", "只留最近 3 条")

    def test_long_detail_truncated(self) -> None:
        events = VoiceEvents()
        events.add("active-tts", ok=False, detail="x" * 1000)
        self.assertLessEqual(len(events.tail()[0]["detail"]), 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
