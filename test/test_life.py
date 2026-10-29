from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

from core import storage
from core.life import Life
from support import NOW, TZ, TmpDirCase, group_session, private_session


def _settings(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        timezone="Asia/Shanghai",
        life=SimpleNamespace(
            enabled=enabled,
            provider_id="",
            character="喜欢散步和读书",
            timeout_seconds=2,
            retry_minutes=1,
            max_attempts=3,
        ),
        state={"wake_time": "08:00", "sleep_time": "23:00", "private_note": "不发送"},
        scope=SimpleNamespace(mode="private"),
        love_peers=["10001"],
        zone=lambda: TZ,
    )


def _plan(date_text: str, activities: list[dict[str, str]]) -> str:
    return json.dumps({"date": date_text, "activities": activities}, ensure_ascii=False)


def _activities(*, first: str = "08:00", second: str = "22:00") -> list[dict[str, str]]:
    return [
        {"start": first, "end": "09:00", "title": "沿河散步", "location": "河边"},
        {"start": second, "end": "23:00", "title": "读一本书", "location": "家中"},
    ]


class LifeTests(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = _settings()
        self.layout = storage.Layout(self.root).ensure()
        self.locks = storage.KeyedLocks()
        self.life = Life(self.settings, self.layout, self.locks)

    def test_failed_manual_replan_does_not_restart_automatic_planning(self) -> None:
        async def scenario():
            async def first(_prompt):
                return _plan(NOW.date().isoformat(), _activities())
            await self.life.tick(first, now=NOW)
            original = self.life.snapshot(now=NOW)["activities"]
            async def broken(_prompt):
                return "bad json"
            self.assertFalse((await self.life.tick(broken, now=NOW, force=True))["ok"])
            calls = []
            async def should_not_run(_prompt):
                calls.append(True)
                return _plan(NOW.date().isoformat(), _activities(first="07:00"))
            await self.life.tick(should_not_run, now=NOW + timedelta(minutes=3))
            self.assertEqual(calls, [])
            self.assertEqual(self.life.snapshot(now=NOW)["activities"], original)
        asyncio.run(scenario())

    def test_replan_cannot_make_protected_history_exceed_the_daily_limit(self) -> None:
        async def scenario():
            past = [{"start": f"{i:02}:00", "end": f"{i+1:02}:00", "title": f"旧活动{i}", "location": "家里"} for i in range(12)]
            async def first(_prompt):
                return _plan(NOW.date().isoformat(), past)
            self.assertTrue((await self.life.tick(first, now=NOW))["ok"])
            original = self.life.snapshot(now=NOW)["activities"]
            future = [{"start": f"22:{i*5:02}", "end": "23:00" if i == 11 else f"22:{(i+1)*5:02}", "title": f"新活动{i}", "location": "家里"} for i in range(12)]
            async def second(_prompt):
                return _plan(NOW.date().isoformat(), future)
            result = await self.life.tick(second, now=NOW, force=True)
            self.assertFalse(result["ok"])
            self.assertTrue(self.life.snapshot(now=NOW)["ok"])
            self.assertEqual(self.life.snapshot(now=NOW)["activities"], original)
        asyncio.run(scenario())

    def test_concurrent_generation_restart_and_next_day(self) -> None:
        calls = 0

        async def planner(prompt: str) -> str:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            self.assertIn("虚拟角色", prompt)
            self.assertNotIn("不发送", prompt)
            return _plan(NOW.date().isoformat(), _activities())

        async def scenario() -> None:
            first, second = await asyncio.gather(
                self.life.tick(planner, now=NOW),
                self.life.tick(planner, now=NOW),
            )
            self.assertEqual(calls, 1)
            self.assertTrue(first["ok"] or second["ok"])
            restarted = Life(self.settings, self.layout, storage.KeyedLocks())
            reused = await restarted.tick(planner, now=NOW)
            self.assertTrue(reused["ok"])
            self.assertEqual(calls, 1)
            next_day = NOW + timedelta(days=1)

            async def tomorrow(prompt: str) -> str:
                nonlocal calls
                calls += 1
                return _plan(next_day.date().isoformat(), _activities())

            result = await restarted.tick(tomorrow, now=next_day)
            self.assertTrue(result["ok"])
            self.assertEqual(result["snapshot"]["date"], next_day.date().isoformat())
            self.assertEqual(calls, 2)

        asyncio.run(scenario())

    def test_bad_json_and_invalid_forced_plan_never_replace_existing_plan(self) -> None:
        async def scenario() -> None:
            async def valid(prompt: str) -> str:
                return _plan(NOW.date().isoformat(), _activities())

            self.assertTrue((await self.life.tick(valid, now=NOW))["ok"])
            original = self.life.snapshot(now=NOW)["activities"]

            async def overlapping(prompt: str) -> str:
                return _plan(
                    NOW.date().isoformat(),
                    [
                        {"start": "10:00", "end": "12:00", "title": "整理房间", "location": "家中"},
                        {"start": "11:00", "end": "13:00", "title": "准备晚餐", "location": "家中"},
                    ],
                )

            failed = await self.life.tick(overlapping, now=NOW, force=True)
            self.assertFalse(failed["ok"])
            self.assertEqual(self.life.snapshot(now=NOW)["activities"], original)

            async def invalid_time(prompt: str) -> str:
                return _plan(
                    NOW.date().isoformat(),
                    [{"start": "25:00", "end": "26:00", "title": "无效时间", "location": "家中"}],
                )

            later = NOW + timedelta(minutes=2)
            failed_time = await self.life.tick(invalid_time, now=later, force=True)
            self.assertFalse(failed_time["ok"])
            self.assertEqual(self.life.snapshot(now=later)["activities"], original)

        asyncio.run(scenario())

        self.layout.life.write_text("{broken", encoding="utf-8")
        bad_snapshot = self.life.snapshot(now=NOW)
        self.assertFalse(bad_snapshot["ok"])
        self.assertFalse(asyncio.run(self.life.replace_future(
            [], expected_revision=bad_snapshot["revision"], now=NOW,
            session=private_session("10001"),
        ))["ok"])
        self.assertEqual(self.layout.life.read_text(encoding="utf-8"), "{broken")

        future_schema = json.dumps({
            "schema": 1,
            "schema_version": storage.SCHEMA_VERSION + 1,
            "revision": 0,
            "days": {},
        })
        self.layout.life.write_text(future_schema, encoding="utf-8")
        future_snapshot = self.life.snapshot(now=NOW)
        self.assertFalse(future_snapshot["ok"])
        self.assertFalse(asyncio.run(self.life.replace_future(
            [], expected_revision=0, now=NOW, session=private_session("10001"),
        ))["ok"])
        self.assertEqual(self.layout.life.read_text(encoding="utf-8"), future_schema)

    def test_late_generation_cannot_overwrite_manual_edit(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def planner(prompt: str) -> str:
            entered.set()
            await release.wait()
            return _plan(NOW.date().isoformat(), _activities())

        async def scenario() -> None:
            pending = asyncio.create_task(self.life.tick(planner, now=NOW))
            await entered.wait()
            revision = self.life.snapshot(now=NOW)["revision"]
            edit = await self.life.replace_future(
                [{"start": "23:00", "end": "24:00", "title": "准备休息", "location": "家中"}],
                expected_revision=revision,
                now=NOW,
                session=private_session("10001"),
            )
            self.assertTrue(edit["ok"])
            release.set()
            late = await pending
            self.assertFalse(late["ok"])
            self.assertTrue(late.get("conflict"))
            current = self.life.snapshot(now=NOW)
            self.assertEqual([item["title"] for item in current["activities"]], ["准备休息"])

        asyncio.run(scenario())

    def test_delayed_result_crossing_midnight_is_discarded(self) -> None:
        late_now = NOW.replace(hour=23, minute=59, second=59, microsecond=0)

        async def planner(prompt: str) -> str:
            await asyncio.sleep(1.05)
            return _plan(late_now.date().isoformat(), _activities())

        result = asyncio.run(self.life.tick(planner, now=late_now))
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("conflict"))
        self.assertEqual(result["snapshot"]["date"], (late_now + timedelta(seconds=1.05)).date().isoformat())
        self.assertEqual(result["snapshot"]["activities"], [])

    def test_attempt_limit_stops_writing_after_terminal_failure(self) -> None:
        self.settings.life.max_attempts = 1
        calls = 0

        async def planner(prompt: str) -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("provider unavailable")

        async def scenario() -> None:
            first = await self.life.tick(planner, now=NOW)
            self.assertFalse(first["ok"])
            later = NOW + timedelta(minutes=2)
            second = await self.life.tick(planner, now=later)
            third = await self.life.tick(planner, now=later)
            self.assertFalse(second["ok"])
            self.assertFalse(third["ok"])
            self.assertEqual(second["snapshot"]["revision"], third["snapshot"]["revision"])
            self.assertEqual(second["snapshot"]["generation"]["attempts"], 1)
            self.assertEqual(second["snapshot"]["generation"]["max_attempts"], 1)
            self.assertEqual(calls, 1)

        asyncio.run(scenario())

    def test_owner_private_access_and_preserve_confirmed_past(self) -> None:
        async def scenario() -> None:
            async def planner(prompt: str) -> str:
                return _plan(NOW.date().isoformat(), _activities())

            self.assertTrue((await self.life.tick(planner, now=NOW))["ok"])
            snapshot = self.life.snapshot(now=NOW)
            first = snapshot["activities"][0]
            denied_group = await self.life.record(
                first["id"], status="done", expected_revision=snapshot["revision"],
                now=NOW, session=group_session(),
            )
            denied_non_owner = await self.life.replace_future(
                [], expected_revision=snapshot["revision"], now=NOW,
                session=private_session("99999"),
            )
            self.assertFalse(denied_group["ok"])
            self.assertFalse(denied_non_owner["ok"])

            confirmed = await self.life.record(
                first["id"], outcome="沿河走了一圈", status="done",
                expected_revision=snapshot["revision"], now=NOW,
                session=private_session("10001"),
            )
            self.assertTrue(confirmed["ok"])
            revision = confirmed["snapshot"]["revision"]
            edited = await self.life.replace_future(
                [{"start": "23:00", "end": "24:00", "title": "整理书桌", "location": "家中"}],
                expected_revision=revision,
                now=NOW,
                session=private_session("10001"),
            )
            self.assertTrue(edited["ok"])
            rows = edited["snapshot"]["activities"]
            self.assertEqual(rows[0]["status"], "done")
            self.assertEqual(rows[0]["outcome"], "沿河走了一圈")
            self.assertEqual([row["title"] for row in rows], ["沿河散步", "整理书桌"])

        asyncio.run(scenario())

    def test_disabled_cancelled_generation_and_elapsed_is_not_done(self) -> None:
        calls = 0

        async def planner(prompt: str) -> str:
            nonlocal calls
            calls += 1
            return _plan(NOW.date().isoformat(), _activities())

        async def scenario() -> None:
            self.settings.life.enabled = False
            disabled = await self.life.tick(planner, now=NOW)
            self.assertFalse(disabled["ok"])
            self.assertEqual(disabled["snapshot"]["generation"]["status"], "disabled")
            self.assertEqual(calls, 0)
            self.assertEqual(self.life.context_line(now=NOW), "")

            self.settings.life.enabled = True
            entered = asyncio.Event()

            async def slow(prompt: str) -> str:
                entered.set()
                await asyncio.Event().wait()
                return "{}"

            task = asyncio.create_task(self.life.tick(slow, now=NOW))
            await entered.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            after_cancel = self.life.snapshot(now=NOW)
            self.assertEqual(after_cancel["generation"]["status"], "failed")
            self.assertEqual(after_cancel["generation"]["attempts"], 1)

            # Use a new day so the cancellation retry window does not affect this plan.
            later = NOW + timedelta(days=1)

            async def next_plan(prompt: str) -> str:
                return _plan(later.date().isoformat(), [
                    {"start": "08:00", "end": "09:00", "title": "花园散步", "location": "花园"},
                    {"start": "22:00", "end": "23:00", "title": "读书", "location": "家中"},
                ])

            self.assertTrue((await self.life.tick(next_plan, now=later))["ok"])
            view = self.life.snapshot(now=later)
            self.assertEqual([item["status"] for item in view["activities"]], ["elapsed", "planned"])
            self.assertIsNone(view["current"])
            result = await self.life.record(
                view["activities"][0]["id"], outcome="散步后心情舒畅", status="done",
                expected_revision=view["revision"], now=later,
                session=private_session("10001"),
            )
            self.assertEqual(result["snapshot"]["activities"][0]["status"], "done")
            line = self.life.context_line(now=later, budget=80)
            self.assertLessEqual(len(line), 80)
            self.assertIn("散步后心情舒畅", line)

        asyncio.run(scenario())
