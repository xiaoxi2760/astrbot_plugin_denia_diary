"""Ownership regression cases independently found during P0 review."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from support import TmpDirCase
from core.runtime import DiaryRuntimeManager, INSTANCE_FILE, scope_dir_name


class RuntimeOwnership(TmpDirCase):
    def manager(self, owner="personal", root=None):
        return DiaryRuntimeManager(root or self.root, {
            "bot_isolation": {"enable": True, "legacy_platform_id": owner},
        })

    def test_explicit_legacy_owner_is_persisted_and_cannot_be_rebound(self):
        first = self.manager()
        self.assertIsNotNone(first.resolve("personal"))
        data = json.loads((self.root / INSTANCE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["scope_id"], "personal")
        self.assertEqual(data["kind"], "legacy")
        second = self.manager(owner="official")
        self.assertIsNone(second.resolve("official"))
        self.assertEqual(data, json.loads((self.root / INSTANCE_FILE).read_text(encoding="utf-8")))

    def test_damaged_legacy_metadata_is_preserved_and_refused(self):
        path = self.root / INSTANCE_FILE
        path.write_text("damaged", encoding="utf-8")
        self.assertIsNone(self.manager().resolve("personal"))
        self.assertEqual(path.read_text(encoding="utf-8"), "damaged")

    def test_unbound_legacy_cannot_be_obtained_through_compatibility_property(self):
        manager = self.manager(owner="")
        with self.assertRaises(ValueError):
            _ = manager.legacy_runtime

    def test_cached_instance_rechecks_metadata(self):
        manager = self.manager()
        official = manager.resolve("official")
        path = official.layout.base_dir / INSTANCE_FILE
        path.write_text("bad", encoding="utf-8")
        self.assertIsNone(manager.resolve("official"))

    def test_event_scope_must_match_umo_instance(self):
        manager = self.manager()
        conflicting = SimpleNamespace(get_platform_id=lambda: "official",
                                      unified_msg_origin="personal:FriendMessage:u")
        self.assertIsNone(manager.resolve_event(conflicting))
        self.assertFalse((self.root / "bots").exists())

    def test_live_route_rebind_is_refused_without_changing_configuration(self):
        manager = self.manager()
        manager.resolve("personal")
        with self.assertRaises(ValueError):
            manager.update_config({"bot_isolation": {"enable": True, "legacy_platform_id": "official"}})
        self.assertEqual(manager.legacy_platform_id, "personal")
        self.assertIsNotNone(manager.resolve("personal"))

    def test_invalid_metadata_version_bool_is_not_version_one(self):
        path = self.root / INSTANCE_FILE
        path.write_text(json.dumps({"schema_version": True, "kind": "legacy", "scope_id": "personal"}), encoding="utf-8")
        self.assertIsNone(self.manager().resolve("personal"))

    def test_configuration_and_snapshots_are_not_mutable_aliases(self):
        source = {"bot_isolation": {"enable": True, "legacy_platform_id": "personal"},
                  "per_instance": {"official": {"love_peers": ["owner"]}}}
        manager = DiaryRuntimeManager(self.root, source)
        source["per_instance"]["official"]["love_peers"].append("attacker")
        snapshot = manager.raw_config
        snapshot["per_instance"]["official"]["love_peers"].append("another")
        self.assertEqual(manager.resolve("official").settings.love_peers, ("owner",))
