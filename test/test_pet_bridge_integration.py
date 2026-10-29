"""主适配层的设备权限、独立情绪时效与监听生命周期验收。"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_main import PluginCase
from support import NOW, private_session


class PetBridgeIntegration(PluginCase):
    def plugin(self, **extra):
        return self.make_plugin({"love_peers": ["10001"], "life": {"enabled": False},
                                 "pet_bridge": {"enabled": True}, **extra})

    def issue(self, plugin):
        result = asyncio.run(plugin.pet_device_issue({"name": "我的电脑", "principal_id": "10001"}))
        self.assertTrue(result["ok"], result)
        return result

    def test_issue_authenticate_revoke_and_no_list_secret(self):
        plugin = self.plugin()
        issued = self.issue(plugin)
        binding = plugin._pet_authenticate(issued["credential"])
        self.assertEqual(binding["principal_id"], "10001")
        listing = json.dumps(plugin.pet_devices(), ensure_ascii=False)
        self.assertNotIn(issued["credential"], listing)
        self.assertNotIn("credential_hash", listing)
        result = asyncio.run(plugin.pet_device_revoke({"device_id": issued["device_id"]}))
        self.assertTrue(result["ok"])
        self.assertIsNone(plugin._pet_authenticate(issued["credential"]))

    def test_unconfigured_principal_cannot_receive_device(self):
        plugin = self.plugin()
        result = asyncio.run(plugin.pet_device_issue({"name": "冒认", "principal_id": "someone-else"}))
        self.assertFalse(result["ok"])
        self.assertEqual(plugin.pet_devices()["devices"], [])

    def test_change_role_or_owner_invalidates_existing_binding(self):
        for change in [{"pet_bridge": {"enabled": True, "agent_id": "other"}}, {"love_peers": []}, {"enabled": False}]:
            plugin = self.plugin()
            issued = self.issue(plugin)
            binding = plugin._pet_authenticate(issued["credential"])
            config = {"love_peers": ["10001"], "pet_bridge": {"enabled": True}, **change}
            plugin.settings = self.module.settings_mod.load_settings(config)
            self.assertIsNone(plugin._pet_authenticate(issued["credential"]))
            with self.assertRaises(PermissionError):
                plugin._pet_snapshot(binding)

    def test_disabled_listener_does_not_import_or_open_network(self):
        plugin = self.plugin(pet_bridge={"enabled": False})
        with patch.object(plugin._pet_server, "start", side_effect=AssertionError("不应监听")):
            asyncio.run(plugin._setup_pet_bridge())
        self.assertFalse(plugin.pet_devices()["listener"]["running"])

    def test_listener_start_failure_is_visible_without_breaking_plugin(self):
        plugin = self.plugin()
        async def fail(*args):
            raise OSError("含内部路径的错误不可直接展示")
        with patch.object(plugin._pet_server, "start", side_effect=fail):
            asyncio.run(plugin._setup_pet_bridge())
        status = plugin.pet_devices()["listener"]
        self.assertFalse(status["running"])
        self.assertTrue(status["error"])
        self.assertNotIn("内部路径", status["error"])

    def test_corrupt_device_file_keeps_plugin_available_and_data_intact(self):
        plugin = self.plugin()
        path = plugin.layout.base_dir / "pet-devices.json"
        path.write_bytes(b'\xffbad')
        self.assertFalse(plugin.pet_devices()["ok"])
        result = asyncio.run(plugin.pet_device_issue({"name": "电脑", "principal_id": "10001"}))
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_bytes(), b'\xffbad')

    def test_public_word_age_is_not_extended_by_coordinate_update(self):
        plugin = self.plugin()
        async def updates():
            await plugin.state.observe(private_session(), word="开心", now=NOW)
            await plugin.state.observe(private_session(), valence=0.9, now=NOW + timedelta(hours=5))
        asyncio.run(updates())
        view = plugin.state.public_mood(now=NOW + timedelta(hours=6, seconds=1))
        self.assertEqual(view["status"], "stale")
        self.assertIsNone(view["word"])
        self.assertEqual(view["observed_at"], NOW.isoformat())

    def test_word_only_update_keeps_its_own_timestamp(self):
        plugin = self.plugin()
        asyncio.run(plugin.state.observe(private_session(), word="安心", now=NOW))
        view = plugin.state.public_mood(now=NOW + timedelta(minutes=30))
        self.assertEqual(view["status"], "fresh")
        self.assertEqual(view["word"], "安心")
        self.assertEqual(view["valid_until"], (NOW + timedelta(hours=6)).isoformat())

    def test_legacy_word_age_survives_coordinate_update(self):
        plugin = self.plugin()
        old = NOW - timedelta(hours=7)
        asyncio.run(plugin.state.store.update_mood(lambda _: {"word": "旧心情", "updated_at": old.isoformat()}))
        asyncio.run(plugin.state.observe(private_session(), valence=1, now=NOW))
        view = plugin.state.public_mood(now=NOW)
        self.assertEqual(view["status"], "stale")
        self.assertEqual(view["observed_at"], old.isoformat())
        self.assertIsNone(view["word"])

    def test_failed_rebind_retains_old_listener_and_reports_actual_address(self):
        plugin = self.plugin()
        async def run():
            await plugin._pet_server.start("127.0.0.1", 0)
            old_server = plugin._pet_server
            old_port = old_server.status()["port"]
            self.assertEqual(plugin.pet_devices()["listener"]["port"], old_port)
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                port = occupied.getsockname()[1]
                plugin.settings = self.module.settings_mod.load_settings({"pet_bridge": {"enabled": True, "port": port}})
                await plugin._setup_pet_bridge()
                self.assertIs(plugin._pet_server, old_server)
                status = plugin.pet_devices()["listener"]
                self.assertTrue(status["running"])
                self.assertEqual(status["port"], old_port)
                self.assertEqual(status["configured_port"], port)
                self.assertTrue(status["error"])
            await plugin.terminate()
        asyncio.run(run())

    def test_future_word_is_not_displayed_as_fresh(self):
        plugin = self.plugin()
        asyncio.run(plugin.state.observe(private_session(), word="开心", now=NOW + timedelta(hours=1)))
        self.assertEqual(plugin.state.public_mood(now=NOW)["status"], "empty")

    def test_corrupt_state_is_unavailable_and_preserved(self):
        plugin = self.plugin()
        plugin.layout.state.write_bytes(b'\xffbroken')
        self.assertEqual(plugin.state.public_mood(now=NOW)["status"], "unavailable")
        self.assertEqual(plugin.layout.state.read_bytes(), b'\xffbroken')

    def test_http_snapshot_and_terminate_release_port(self):
        import aiohttp
        plugin = self.plugin()
        issued = self.issue(plugin)
        async def run():
            await plugin._pet_server.start("127.0.0.1", 0)
            status = plugin._pet_server.status()
            self.assertTrue(status["running"], status)
            port = status["port"]
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{port}/api/pet/v1/snapshot", headers={"Authorization": "Bearer " + issued["credential"]}) as response:
                    self.assertEqual(response.status, 200)
                    payload = await response.json()
                    self.assertEqual(payload["agent_id"], "yaya")
                    self.assertEqual(payload["life"]["status"], "disabled")
                    self.assertNotIn("principal_id", payload)
            await plugin.terminate()
            self.assertFalse(plugin._pet_server.status()["running"])
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        asyncio.run(run())

    def test_invalid_listener_config_is_disabled(self):
        load = self.module.settings_mod.load_settings
        for config in [{"host": "http://bad"}, {"port": True}, {"port": 80}, {"agent_id": "../oops"}]:
            settings = load({"pet_bridge": {"enabled": True, **config}})
            self.assertFalse(settings.pet_bridge["enabled"])
            self.assertTrue(settings.warnings)
