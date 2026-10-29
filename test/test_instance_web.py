"""Real main + handlers: the administrator's selected scope owns every operation."""
from __future__ import annotations

import asyncio
import copy
import types
from unittest.mock import patch

from test_main import PluginCase
from test_instance_acceptance import event
from web_api import handlers


class InstanceWeb(PluginCase):
    def plugin(self):
        return self.make_plugin({
            "bot_isolation": {"enable": True, "legacy_platform_id": "personal"},
            "life": {"enabled": False},
            "per_instance": {"official": {"diary": {"max_chars": 1400}}},
            "pet_bridge": {"enabled": False},
        })

    async def call(self, plugin, name, scope="personal", body=None, query=None):
        async def json(default=None):
            return body if body is not None else default
        query = {"scope_id": scope} if query is None else query
        request = types.SimpleNamespace(query=query, json=json, username="synthetic-admin")
        with patch.object(handlers, "request", request):
            return await handlers.build_handlers(plugin)[name]()

    def test_scope_required_unknown_and_conflict_do_not_create_runtime(self):
        async def run():
            plugin = self.plugin()
            for query, body in [({}, None), ({"scope_id": "unknown"}, None),
                                ({"scope_id": "personal"}, {"scope_id": "official", "changes": {"timezone": "UTC"}})]:
                name = "settings_post" if body else "settings_get"
                result = await self.call(plugin, name, body=body, query=query)
                self.assertEqual(result["status"], 400)
            self.assertFalse((self.root / "data" / "bots").exists())
        asyncio.run(run())

    def test_selected_settings_save_only_changed_leaves(self):
        async def run():
            plugin = self.plugin()
            self.assertTrue((await self.call(plugin, "settings_post", "official", {
                "scope_id": "official", "changes": {"diary.card_style": "ink"},
            }))["data"]["ok"])
            self.assertEqual(plugin.config["per_instance"]["official"],
                             {"diary": {"max_chars": 1400, "card_style": "ink"}})
            self.assertNotIn("diary", {k: v for k, v in plugin.config.items() if k != "per_instance"})
            self.assertEqual(plugin.resolve_runtime("personal").settings.diary["card_style"], "paper")
            self.assertEqual(plugin.resolve_runtime("official").settings.diary["card_style"], "ink")
            self.assertIsNone(plugin._bound_runtime())
        asyncio.run(run())

    def test_host_fields_are_readonly_and_rejected_without_changes(self):
        async def run():
            plugin = self.plugin()
            response = await self.call(plugin, "settings_get")
            fields = {item["path"]: item for item in response["data"]["fields"]}
            self.assertFalse(fields["bot_isolation.enable"]["editable"])
            self.assertFalse(fields["enabled"]["editable"])
            before = copy.deepcopy(plugin.config)
            response = await self.call(plugin, "settings_post", body={
                "scope_id": "personal", "changes": {"enabled": False},
            })
            self.assertEqual(response["status"], 400)
            self.assertEqual(plugin.config, before)
        asyncio.run(run())

    def test_diary_api_and_device_api_follow_selected_scope(self):
        async def run():
            plugin = self.plugin()
            await plugin.diary_write(event("personal"), "PERSONAL_WEB")
            await plugin.diary_write(event("official"), "OFFICIAL_WEB")
            for scope, own, other in [("personal", "PERSONAL_WEB", "OFFICIAL_WEB"),
                                      ("official", "OFFICIAL_WEB", "PERSONAL_WEB")]:
                response = await self.call(plugin, "diary_content", scope)
                self.assertIn(own, str(response))
                self.assertNotIn(other, str(response))
            result = await self.call(plugin, "pet_devices", "official")
            self.assertEqual(result["status"], 400)
            with plugin._bind_runtime(plugin.resolve_runtime("official")):
                self.assertFalse(plugin.pet_devices()["ok"])
                self.assertFalse((await plugin.pet_device_issue({"name": "synthetic", "principal_id": "x"}))["ok"])
            self.assertIsNone(plugin._bound_runtime())
        asyncio.run(run())

    def test_hot_route_change_rejected_before_persistence(self):
        async def run():
            plugin = self.plugin()
            plugin.resolve_runtime("personal")
            original = copy.deepcopy(plugin.config)
            proposed = copy.deepcopy(original)
            proposed["bot_isolation"]["legacy_platform_id"] = "official"
            result = await plugin.apply_settings(proposed)
            self.assertFalse(result["ok"])
            self.assertEqual(plugin.config, original)
            self.assertEqual(plugin._manager.legacy_platform_id, "personal")
        asyncio.run(run())

    def test_same_scope_parallel_saves_do_not_freeze_inherited_fields(self):
        async def run():
            plugin = self.plugin()
            # Both forms started from the same effective snapshot.
            await asyncio.gather(
                plugin.apply_scope_settings("official", {"diary": {"max_chars": 1600}}, applied=["diary.max_chars"]),
                plugin.apply_scope_settings("official", {"diary": {"max_chars": 1400, "card_style": "ink"}}, applied=["diary.card_style"]),
            )
            self.assertEqual(plugin.config["per_instance"]["official"], {"diary": {"max_chars": 1600, "card_style": "ink"}})
        asyncio.run(run())

    def test_style_command_updates_only_its_instance(self):
        async def run():
            plugin = self.plugin()
            source = event("official")
            source.plain_result = lambda text: text
            async for _ in plugin.diary_card_style(source, "ink"):
                pass
            self.assertEqual(plugin.resolve_runtime("official").settings.diary["card_style"], "ink")
            self.assertEqual(plugin.resolve_runtime("personal").settings.diary["card_style"], "paper")
            self.assertNotIn("diary", plugin.config)
        asyncio.run(run())
