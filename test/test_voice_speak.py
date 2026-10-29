"""``core/voice/speak``（有界合成）的行为测试：钉死每个边界——成功 / 异常 / 永不返回 /
取消 / 总预算 / 分段上限 / 落地校验。红线：任何失败都只降级为文字，绝不吞取消信号。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
sys.path.insert(0, str(_P(__file__).resolve().parent))

from core.voice.speak import (  # noqa: E402
    clamp_config,
    clamp_count,
    clamp_seconds,
    synthesize_segments,
)
from core.voice.plan import apply_audio  # noqa: E402


class _OkProvider:
    """每段都"合成"出一个可校验的假路径。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.calls.append(text)
        return f"/tmp/fake/{len(self.calls)}.mp3"


class _BoomProvider:
    async def get_audio(self, text: str) -> str:
        raise RuntimeError("tts down")


class _NeverProvider:
    """永不返回（宿主网络挂死的忠实替身）：比超时长得多。"""

    async def get_audio(self, text: str) -> str:
        await asyncio.sleep(30)
        return "/tmp/fake/never.mp3"  # pragma: no cover - 到不了


class _SlowProvider:
    """真实慢速 provider：真等 wait_for 的超时（0.05 秒级，测试够快）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.calls.append(text)
        await asyncio.sleep(1.0)
        return "/tmp/fake/slow.mp3"  # pragma: no cover - 到不了


class _CancellationResistantProvider:
    """测试宿主 provider 吞取消：必须在事件释放后才退出。"""

    def __init__(self, *, fail_after_release: bool = False) -> None:
        self.entered = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls: list[str] = []
        self.active = 0
        self.fail_after_release = fail_after_release

    async def get_audio(self, text: str) -> str:
        self.calls.append(text)
        self.active += 1
        self.entered.set()
        try:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.release.wait()  # 忽略取消，模拟问题 provider
            if self.fail_after_release:
                raise RuntimeError("late provider failure")
            return "/tmp/fake/released.mp3"
        finally:
            self.active -= 1
            self.finished.set()


class _BudgetProvider:
    """消耗注入时钟的 provider：预算测试不靠真实时间（确定性强）。"""

    def __init__(self, clock: list[float], cost: float) -> None:
        self.clock = clock
        self.cost = cost
        self.calls: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.clock[0] += self.cost
        self.calls.append(text)
        await asyncio.sleep(0)  # 让出事件循环（wait_for 正常返回）
        return f"/tmp/fake/{len(self.calls)}.mp3"


class SynthesizeSegmentsTest(unittest.TestCase):
    TARGETS = [(0, "早安"), (2, "晚安")]

    def test_success_returns_paths(self) -> None:
        provider = _OkProvider()
        audio, failures = asyncio.run(
            synthesize_segments(provider, self.TARGETS, verify_path=lambda p: True)
        )
        self.assertEqual(audio, {0: "/tmp/fake/1.mp3", 2: "/tmp/fake/2.mp3"})
        self.assertEqual(failures, {})
        self.assertEqual(provider.calls, ["早安", "晚安"])

    def test_exception_per_segment_keeps_others(self) -> None:
        class _FirstBoom:
            async def get_audio(self, text: str) -> str:
                if text == "早安":
                    raise RuntimeError("tts down")
                return "/tmp/fake/b.mp3"

        audio, failures = asyncio.run(
            synthesize_segments(_FirstBoom(), self.TARGETS, verify_path=lambda p: True)
        )
        self.assertEqual(audio, {2: "/tmp/fake/b.mp3"}, "单段失败不连累其余")
        self.assertEqual(list(failures), [0])
        self.assertIn("tts down", failures[0])

    def test_never_returning_provider_hits_timeout(self) -> None:
        provider = _NeverProvider()
        audio, failures = asyncio.run(
            synthesize_segments(
                provider, self.TARGETS, per_call_timeout=0.05, total_budget=10.0,
                verify_path=lambda p: True,
            )
        )
        self.assertEqual(audio, {}, "永不返回 = 超时降级，绝不挂死整条消息")
        self.assertEqual(list(failures), [0, 2])
        self.assertIn("超时", failures[0])

    def test_swallowed_cancel_returns_bounded_and_skips_overlapping_segments(self) -> None:
        provider = _CancellationResistantProvider(fail_after_release=True)

        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            loop_errors: list[dict[str, object]] = []
            loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
            task = asyncio.create_task(
                synthesize_segments(
                    provider,
                    [(0, "一"), (1, "二")],
                    per_call_timeout=0.02,
                    total_budget=1.0,
                )
            )
            await asyncio.wait_for(provider.entered.wait(), timeout=0.2)
            try:
                audio, failures = await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
                self.assertEqual(audio, {})
                self.assertIn("超时", failures[0])
                self.assertIn("跳过", failures[1])
                self.assertEqual(provider.calls, ["一"], "未停止的 provider 不得与下一段重叠")
                self.assertTrue(provider.cancel_seen.is_set())
                self.assertEqual(provider.active, 1, "返回时只剩一个已取消的后台任务")
                duplicate_audio, duplicate_failures = await synthesize_segments(
                    provider, [(2, "三")], per_call_timeout=0.02, total_budget=1.0
                )
                self.assertEqual(duplicate_audio, {})
                self.assertIn("仍有未结束调用", duplicate_failures[2])
                self.assertEqual(provider.calls, ["一"], "后续事件也不得重叠调用该 provider")
            finally:
                # 即使断言失败也释放抵抗取消的假 provider，测试本身不会挂住。
                provider.release.set()
                if not task.done():
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
                await asyncio.wait_for(provider.finished.wait(), timeout=0.3)
                await asyncio.sleep(0)
            self.assertEqual(loop_errors, [], "后台异常应由回收回调取走")

        asyncio.run(scenario())

    def test_cancelled_error_propagates(self) -> None:
        provider = _NeverProvider()

        async def scenario() -> None:
            task = asyncio.ensure_future(
                synthesize_segments(provider, self.TARGETS, per_call_timeout=10.0)
            )
            await asyncio.sleep(0.05)  # 让它进到 get_audio 的 sleep 里
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_host_cancellation_bubbles_up_without_waiting_for_provider_cleanup(self) -> None:
        provider = _CancellationResistantProvider()

        async def scenario() -> None:
            task = asyncio.create_task(
                synthesize_segments(provider, self.TARGETS, per_call_timeout=2.0)
            )
            await asyncio.wait_for(provider.entered.wait(), timeout=0.2)
            task.cancel()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=0.2)
                self.assertTrue(provider.cancel_seen.is_set())
                self.assertFalse(provider.finished.is_set(), "宿主取消不能等待 provider 清理")
            finally:
                provider.release.set()
                await asyncio.wait_for(provider.finished.wait(), timeout=0.3)

        asyncio.run(scenario())

    def test_total_budget_stops_later_segments(self) -> None:
        clock = [100.0]
        provider = _BudgetProvider(clock, cost=0.4)  # 每段耗 0.4，预算 0.8 只够两段开跑
        audio, failures = asyncio.run(
            synthesize_segments(
                provider,
                [(0, "一"), (1, "二"), (2, "三"), (3, "四")],
                per_call_timeout=5.0,
                total_budget=0.8,
                clock=lambda: clock[0],
            )
        )
        self.assertEqual(sorted(audio), [0, 1], "预算内的段照常成功")
        self.assertEqual(sorted(failures), [2, 3], "预算外的段整批放弃")
        self.assertIn("预算", failures[2])
        self.assertEqual(provider.calls, ["一", "二"], "被放弃的段不再调 provider")

    def test_segment_cap_skips_without_calling(self) -> None:
        provider = _OkProvider()
        audio, failures = asyncio.run(
            synthesize_segments(
                provider,
                [(0, "一"), (1, "二"), (2, "三")],
                max_segments=2, verify_path=lambda p: True,
            )
        )
        self.assertEqual(sorted(audio), [0, 1])
        self.assertEqual(list(failures), [2])
        self.assertIn("分段上限", failures[2])
        self.assertEqual(len(provider.calls), 2)

    def test_missing_file_fails_verification(self) -> None:
        provider = _OkProvider()
        audio, failures = asyncio.run(
            synthesize_segments(provider, self.TARGETS, verify_path=lambda p: False)
        )
        self.assertEqual(audio, {})
        self.assertIn("不存在或为空", failures[0])

    def test_empty_path_fails(self) -> None:
        class _EmptyProvider:
            async def get_audio(self, text: str) -> str:
                return "   "

        audio, failures = asyncio.run(
            synthesize_segments(_EmptyProvider(), self.TARGETS, verify_path=lambda p: True)
        )
        self.assertEqual(audio, {})
        self.assertIn("没有返回音频", failures[0])

    def test_no_provider_fails_every_segment(self) -> None:
        audio, failures = asyncio.run(synthesize_segments(None, self.TARGETS))
        self.assertEqual(audio, {})
        self.assertEqual(sorted(failures), [0, 2])
        self.assertIn("提供方", failures[0])


class ClampTest(unittest.TestCase):
    def test_clamp_count(self) -> None:
        self.assertEqual(clamp_count("3"), 3)
        self.assertEqual(clamp_count(0), 1, "下限 1")
        self.assertEqual(clamp_count(99), 8, "上限 8")
        self.assertEqual(clamp_count("乱写"), 3, "非法回落默认")
        self.assertEqual(clamp_count(None), 3)

    def test_clamp_seconds(self) -> None:
        self.assertEqual(clamp_seconds("15", 20.0, (3.0, 120.0)), 15.0)
        self.assertEqual(clamp_seconds(1, 20.0, (3.0, 120.0)), 3.0, "下限夹紧")
        self.assertEqual(clamp_seconds(999, 20.0, (3.0, 120.0)), 120.0, "上限夹紧")
        self.assertEqual(clamp_seconds("乱写", 20.0, (3.0, 120.0)), 20.0)
        self.assertEqual(clamp_seconds(float("nan"), 20.0, (3.0, 120.0)), 20.0, "NaN 回默认")

    def test_clamp_config_reads_voice_group(self) -> None:
        cfg = clamp_config({"max_segments": 5, "tts_timeout_seconds": "30", "tts_total_seconds": 90})
        self.assertEqual(cfg, {"max_segments": 5, "per_call_timeout": 30.0, "total_budget": 90.0})
        bad = clamp_config({"max_segments": "乱", "tts_timeout_seconds": -5, "tts_total_seconds": None})
        self.assertEqual(bad, {"max_segments": 3, "per_call_timeout": 3.0, "total_budget": 60.0})
        empty = clamp_config(None)
        self.assertEqual(empty, {"max_segments": 3, "per_call_timeout": 20.0, "total_budget": 60.0})


class UrlModeTest(unittest.TestCase):
    """``apply_audio(url_mode=True)``：文件服务交接的 record 形（url 键，宿主 fromURL）。"""

    MESSAGES = [{"type": "plain", "text": "早安"}]

    def test_url_mode_emits_url_key(self) -> None:
        out = apply_audio(self.MESSAGES, {0: "http://host/api/file/tok"}, mode="voice", url_mode=True)
        self.assertEqual(out, [{"type": "record", "url": "http://host/api/file/tok"}])

    def test_default_keeps_path_key(self) -> None:
        out = apply_audio(self.MESSAGES, {0: "/tmp/a.mp3"}, mode="dual")
        self.assertEqual(out, [{"type": "plain", "text": "早安"}, {"type": "record", "path": "/tmp/a.mp3"}])

    def test_partial_failure_keeps_failed_segments_as_text(self) -> None:
        """多段部分失败：成功的段转语音，失败的段**完整保留原文**。"""
        messages = [
            {"type": "plain", "text": "第一段"},
            {"type": "plain", "text": "第二段"},
        ]
        out = apply_audio(messages, {0: "/tmp/a.mp3"}, mode="voice")
        self.assertEqual(
            out,
            [{"type": "record", "path": "/tmp/a.mp3"}, {"type": "plain", "text": "第二段"}],
            "失败段的原文一字不丢",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
