"""Background tasks cannot route or clean artifacts on behalf of another bot."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import test_instance_acceptance as acceptance
from test_main import PluginCase


class InstanceBackground(PluginCase):
    plugin = acceptance.InstanceAcceptance.plugin
    runtime = acceptance.InstanceAcceptance.runtime
    def test_patrol_rejects_foreign_session_before_delivery(self):
        async def run():
            plugin = self.plugin()
            runtime = self.runtime(plugin, "official")
            decision = {"umo": "personal:FriendMessage:same-person", "kind": "direct", "char": ".", "slot": "test"}
            with patch.object(runtime.proactive, "patrol", AsyncMock(return_value=decision)), \
                 patch.object(plugin, "_send_signal", AsyncMock()) as send, \
                 patch.object(plugin, "_wake", AsyncMock()) as wake:
                await plugin._patrol_runtime(runtime)
                send.assert_not_awaited()
                wake.assert_not_awaited()
        asyncio.run(run())

    def test_corrupt_legacy_does_not_block_other_background_runtime(self):
        plugin = self.plugin()
        legacy = self.runtime(plugin, "personal")
        self.runtime(plugin, "official")
        (legacy.base_dir / "instance.json").write_text("corrupt", encoding="utf-8")
        self.assertEqual([rt.scope for rt in plugin._host_runtimes()], ["official"])

    def test_provider_diagnostics_and_artifact_retention_are_per_instance(self):
        async def run():
            plugin = self.plugin(per_instance={"personal": {"voice": {"audio_retention_hours": 1}},
                                               "official": {"voice": {"audio_retention_hours": 24}}})
            paths = {}
            for scope in ("personal", "official"):
                with plugin._bind_runtime(self.runtime(plugin, scope)):
                    plugin._last_tts_provider = scope
                    base = plugin._voice_cache_dir()
                    base.mkdir(parents=True, exist_ok=True)
                    path = base / (scope + ".wav")
                    path.write_bytes(b"synthetic audio")
                    plugin._voice_artifacts[str(path)] = (plugin._now() - timedelta(hours=2)).isoformat()
                    paths[scope] = path
            with plugin._bind_runtime(self.runtime(plugin, "personal")):
                self.assertEqual(plugin._last_tts_provider, "personal")
            with plugin._bind_runtime(self.runtime(plugin, "official")):
                self.assertEqual(plugin._last_tts_provider, "official")
            await plugin._sweep_voice_artifacts()
            self.assertFalse(paths["personal"].exists())
            self.assertTrue(paths["official"].exists())
            self.assertIn(str(paths["official"]), plugin._voice_artifacts)
        asyncio.run(run())
