"""Patrol explanations use the same scope and gates, without reading private content."""
import asyncio
import unittest
from datetime import timedelta
from unittest.mock import patch

from support import TmpDirCase, private_session
from test_proactive import make_stack, SUN, MON_LATE
from core.proactive import diagnostics
from core import webui_data


class DiagnosticsTest(TmpDirCase):
    def test_no_contacts_and_disabled(self):
        stack = make_stack(self.root)
        self.assertEqual(diagnostics.check_snapshot(stack.proactive, None, now=SUN)["status"], "no_target")
        stack = make_stack(self.root, {"subsystems": {"proactive": False}})
        self.assertEqual(diagnostics.check_snapshot(stack.proactive, None, now=SUN)["status"], "disabled")

    def test_quiet_does_not_override_signal_dispatch(self):
        stack = make_stack(self.root)
        self.assertEqual(diagnostics.check_snapshot(stack.proactive, None, now=MON_LATE)["status"], "quiet")
        decision = {"umo": "aiocqhttp:FriendMessage:10001", "slot": "signal"}
        self.assertEqual(diagnostics.check_snapshot(stack.proactive, decision, now=MON_LATE)["status"], "dispatched")

    def test_quota_interval_and_idle(self):
        stack = make_stack(self.root)
        asyncio.run(stack.proactive.note_contact(private_session()))
        self.assertEqual(diagnostics.check_snapshot(stack.proactive, None, now=SUN)["status"], "idle")
        umo = private_session().umo
        async def seed(record):
            await stack.proactive.store.update(lambda doc: {**doc, "sessions": {umo: record}})
        asyncio.run(seed({"today_date": SUN.date().isoformat(), "today_count": 2}))
        result = diagnostics.check_snapshot(stack.proactive, None, now=SUN)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("配额", result["sessions"][0]["reason"])
        asyncio.run(seed({"last_sent_at": (SUN - timedelta(minutes=1)).isoformat()}))
        self.assertIn("不足", diagnostics.check_snapshot(stack.proactive, None, now=SUN)["sessions"][0]["reason"])

    def test_delivery_persists_and_panel_reads_it_without_touching_counters(self):
        stack = make_stack(self.root)
        asyncio.run(diagnostics.delivery(stack.proactive, "a", success=False, now=SUN))
        payload = webui_data.proactive_payload(proactive_store=stack.proactive.store, now=SUN)
        self.assertEqual(payload["diagnostic"]["status"], "failed")
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(payload["log"], [])
        asyncio.run(diagnostics.delivery(stack.proactive, "a", success=True, now=SUN))
        self.assertEqual(stack.proactive.store.read()["diagnostic"]["status"], "sent")

    def test_malformed_contacts_and_explanation_failure_are_nonfatal(self):
        stack = make_stack(self.root)
        asyncio.run(stack.proactive.store.update(lambda doc: {**doc, "contacts": ["bad"]}))
        self.assertEqual(diagnostics.check_snapshot(stack.proactive, None, now=SUN)["status"], "no_target")
        with patch.object(diagnostics, "check_snapshot", side_effect=RuntimeError("broken explanation")):
            asyncio.run(diagnostics.record_check(stack.proactive, {"umo": "a"}, now=SUN))
        self.assertEqual(stack.proactive.store.read()["diagnostic"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
