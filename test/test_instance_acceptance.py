"""Independent acceptance of real plugin entry points with synthetic identities."""
from __future__ import annotations

import asyncio
import types
from unittest.mock import patch

from test_main import PluginCase, _FakeEvent


def event(scope: str, text: str = "", user: str = "same-person"):
    value = _FakeEvent(f"{scope}:FriendMessage:{user}", sender_id=user, message_str=text)
    value.get_platform_id = lambda: scope
    return value


class InstanceAcceptance(PluginCase):
    def plugin(self, **extra):
        return self.make_plugin({
            "bot_isolation": {"enable": True, "legacy_platform_id": "personal"},
            "life": {"enabled": False},
            "scope": {"mode": "private"},
            **extra,
        })

    def runtime(self, plugin, scope):
        return plugin.resolve_runtime(scope)

    def test_same_person_tools_and_prompt_keep_bot_ownership(self):
        async def run():
            plugin = self.plugin()
            personal, official = event("personal"), event("official")
            await plugin.diary_write(personal, "PERSONAL_ONLY coffee by the window")
            await plugin.diary_write(official, "OFFICIAL_ONLY reading in the library")
            left = await plugin.diary_read(personal)
            right = await plugin.diary_read(official)
            self.assertIn("PERSONAL_ONLY", left)
            self.assertNotIn("OFFICIAL_ONLY", left)
            self.assertIn("OFFICIAL_ONLY", right)
            self.assertNotIn("PERSONAL_ONLY", right)
            left_rt = self.runtime(plugin, "personal")
            right_rt = self.runtime(plugin, "official")
            self.assertNotEqual(left_rt.layout.base_dir, right_rt.layout.base_dir)
            for source, own, other in [(personal, "PERSONAL_ONLY", "OFFICIAL_ONLY"),
                                       (official, "OFFICIAL_ONLY", "PERSONAL_ONLY")]:
                req = types.SimpleNamespace(system_prompt="", extra_user_content_parts=[])
                await plugin.inject_diary_hint(source, req)
                text = req.system_prompt + "".join(str(getattr(part, "text", "")) for part in req.extra_user_content_parts)
                self.assertIn(own, text)
                self.assertNotIn(other, text)
            await plugin.terminate()
        asyncio.run(run())

    def test_interleaved_tool_await_does_not_switch_runtime(self):
        async def run():
            plugin = self.plugin()
            left = self.runtime(plugin, "personal")
            entered, release = asyncio.Event(), asyncio.Event()
            original = left.diary.write_async

            async def slow(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            with patch.object(left.diary, "write_async", side_effect=slow):
                pending = asyncio.create_task(plugin.diary_write(event("personal"), "WAITED_PERSONAL"))
                await entered.wait()
                await plugin.diary_write(event("official"), "FAST_OFFICIAL")
                release.set()
                await pending
            personal = await plugin.diary_read(event("personal"))
            official = await plugin.diary_read(event("official"))
            self.assertIn("WAITED_PERSONAL", personal)
            self.assertNotIn("FAST_OFFICIAL", personal)
            self.assertIn("FAST_OFFICIAL", official)
            self.assertNotIn("WAITED_PERSONAL", official)
            await plugin.terminate()
        asyncio.run(run())

    def test_missing_instance_does_not_write_legacy(self):
        async def run():
            plugin = self.plugin()
            data_root = self.root / "data"
            before = {p.name: p.read_bytes() for p in data_root.glob("*.jsonl")}
            missing = event("")
            result = await plugin.diary_write(missing, "MUST_NOT_BE_STORED")
            self.assertTrue(result)
            self.assertEqual(before, {p.name: p.read_bytes() for p in data_root.glob("*.jsonl")})
            await plugin.terminate()
        asyncio.run(run())

    def test_global_disable_survives_instance_override(self):
        plugin = self.plugin(enabled=False, per_instance={"official": {"enabled": True}})
        rt = self.runtime(plugin, "official")
        self.assertFalse(rt.settings.enabled)

    def test_legacy_diary_is_not_copied_to_official(self):
        async def run():
            old = self.make_plugin({"life": {"enabled": False}})
            await old.diary_write(event("personal"), "OLD_PERSONAL_HISTORY")
            data_root = self.root / "data"
            original = {p.name: p.read_bytes() for p in data_root.glob("*.jsonl")}
            plugin = self.plugin()
            self.assertIn("OLD_PERSONAL_HISTORY", await plugin.diary_read(event("personal")))
            self.assertNotIn("OLD_PERSONAL_HISTORY", await plugin.diary_read(event("official")))
            self.assertEqual(original, {p.name: p.read_bytes() for p in data_root.glob("*.jsonl")})
            await plugin.terminate()
        asyncio.run(run())

    def test_v1_device_cannot_read_or_issue_for_official(self):
        async def run():
            plugin = self.plugin(love_peers=["same-person"], pet_bridge={"enabled": True})
            issued = await plugin.pet_device_issue({"name": "synthetic", "principal_id": "same-person"})
            self.assertTrue(issued["ok"], issued)
            binding = plugin._pet_authenticate(issued["credential"])
            self.assertIsNotNone(binding)
            self.runtime(plugin, "official")
            modified = dict(binding, scope_id="official")
            with self.assertRaises(PermissionError):
                plugin._pet_snapshot(modified)
            await plugin.terminate()
        asyncio.run(run())

    def test_terminate_flushes_all_instances(self):
        async def run():
            plugin = self.plugin()
            for scope in ("personal", "official"):
                req = types.SimpleNamespace(system_prompt="", extra_user_content_parts=[])
                await plugin.inject_diary_hint(event(scope, "hello"), req)
            roots = [self.runtime(plugin, scope).layout.base_dir for scope in ("personal", "official")]
            await plugin.terminate()
            self.assertTrue(all((root / "affinity.json").is_file() for root in roots))
        asyncio.run(run())
