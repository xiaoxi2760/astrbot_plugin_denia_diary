"""语音出口决策层（``core/voice``）的行为测试：全是纯函数，钉死每个分支。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
sys.path.insert(0, str(_P(__file__).resolve().parent))

from core.voice import (  # noqa: E402
    MAX_SPEAK_CHARS,
    apply_audio,
    speak_plan,
    speakable_indices,
)


class SpeakPlanTest(unittest.TestCase):
    def test_off_never_speaks(self) -> None:
        self.assertFalse(speak_plan(mode="off", probability=100, roll=0.0))
        self.assertFalse(speak_plan(mode="", probability=100, roll=0.0))
        self.assertFalse(speak_plan(mode="乱写", probability=100, roll=0.0))

    def test_voice_and_dual_follow_probability(self) -> None:
        for mode in ("voice", "dual"):
            self.assertTrue(speak_plan(mode=mode, probability=100, roll=1.0), mode)
            self.assertFalse(speak_plan(mode=mode, probability=0, roll=0.0), mode)
            self.assertTrue(speak_plan(mode=mode, probability=50, roll=0.49), mode)
            self.assertFalse(speak_plan(mode=mode, probability=50, roll=0.5), mode)

    def test_bad_probability_falls_back_to_full(self) -> None:
        self.assertTrue(speak_plan(mode="voice", probability="乱", roll=1.0))

    def test_negative_valence_boosts_probability(self) -> None:
        """"情绪不好更倾向开口"：+0.3 封顶 100%。"""
        self.assertTrue(
            speak_plan(mode="voice", probability=60, valence=-1, roll=0.85),
            "60% + 30pp = 90% ≥ roll 0.85",
        )
        self.assertFalse(
            speak_plan(mode="voice", probability=60, valence=1, roll=0.65),
            "正情绪不加成",
        )
        self.assertFalse(
            speak_plan(mode="voice", probability=60, valence=None, roll=0.65),
            "没自报过坐标不加成",
        )
        self.assertTrue(
            speak_plan(mode="voice", probability=100, valence=-2, roll=1.0),
            "封顶 100%",
        )


class SpeakableIndicesTest(unittest.TestCase):
    def test_only_plain_nonempty_within_limit(self) -> None:
        messages = [
            {"type": "plain", "text": "早呀"},
            {"type": "plain", "text": "   "},
            {"type": "plain", "text": "x" * (MAX_SPEAK_CHARS + 1)},
            {"type": "image", "path": "/tmp/a.png"},
            {"type": "record", "path": "/tmp/a.mp3"},
            "不是 dict",
            {"type": "plain", "text": "晚安"},
        ]
        self.assertEqual(speakable_indices(messages), [0, 6])

    def test_bad_input_returns_empty(self) -> None:
        self.assertEqual(speakable_indices(None), [])
        self.assertEqual(speakable_indices("早安"), [])
        self.assertEqual(speakable_indices({"type": "plain"}), [])


class ApplyAudioTest(unittest.TestCase):
    MESSAGES = [
        {"type": "plain", "text": "早安"},
        {"type": "mention_user", "mention_user_id": "10001"},
        {"type": "plain", "text": "晚上记得吃饭"},
    ]
    AUDIO = {0: "/tmp/a.mp3", 2: "/tmp/b.mp3"}

    def test_voice_mode_replaces_plain(self) -> None:
        out = apply_audio(self.MESSAGES, self.AUDIO, mode="voice")
        self.assertEqual(
            out,
            [
                {"type": "record", "path": "/tmp/a.mp3"},
                {"type": "mention_user", "mention_user_id": "10001"},
                {"type": "record", "path": "/tmp/b.mp3"},
            ],
        )

    def test_dual_mode_keeps_plain_and_appends_record(self) -> None:
        out = apply_audio(self.MESSAGES, {0: "/tmp/a.mp3"}, mode="dual")
        self.assertEqual(
            out,
            [
                {"type": "plain", "text": "早安"},
                {"type": "record", "path": "/tmp/a.mp3"},
                {"type": "mention_user", "mention_user_id": "10001"},
                {"type": "plain", "text": "晚上记得吃饭"},
            ],
        )

    def test_no_audio_returns_none(self) -> None:
        self.assertIsNone(apply_audio(self.MESSAGES, {}, mode="voice"))
        self.assertIsNone(apply_audio(self.MESSAGES, {1: "/tmp/x.mp3"}, mode="voice"),
                          "下标不是 plain 的音频被忽略，等于没变化")

    def test_bad_mode_returns_none(self) -> None:
        self.assertIsNone(apply_audio(self.MESSAGES, self.AUDIO, mode="off"))
        self.assertIsNone(apply_audio(self.MESSAGES, self.AUDIO, mode=""))

    def test_returns_new_list_without_mutating_input(self) -> None:
        original = [dict(m) for m in self.MESSAGES]
        out = apply_audio(self.MESSAGES, {0: "/tmp/a.mp3"}, mode="voice")
        self.assertIsNot(out, self.MESSAGES)
        self.assertEqual(self.MESSAGES[0], original[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
