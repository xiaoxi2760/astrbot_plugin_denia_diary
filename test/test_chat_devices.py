import json
from datetime import datetime, timezone

from support import TmpDirCase
from core.pet_state.chat_devices import ChatDeviceStore
from core.pet_state.devices import DeviceStore
from core.pet_state.errors import DeviceStoreError


class ChatDevices(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.store = ChatDeviceStore(self.root / "v2-devices.json")
        self.binding = {"scope_id": "personal", "agent_id": "yaya", "platform_subject_id": "original-subject", "preset_id": "original-preset", "namespace": "short"}
        self.now = datetime.now(timezone.utc)

    def issue(self):
        return self.store.issue("synthetic device", self.binding, ["chat:write", "turn:read", "state:read"], self.now)

    def test_one_time_token_is_hashed_and_authenticated_identity_is_immutable(self):
        issued = self.issue()
        credential = issued["credential"]
        disk = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn(credential.encode().decode(), disk)
        self.assertNotIn(credential.encode().decode(), repr(issued))
        binding = self.store.authenticate(credential)
        self.assertEqual(binding["platform_subject_id"], "original-subject")
        self.assertEqual(binding["namespace"], "short")
        self.assertNotIn("token_hash", binding)
        self.assertTrue(self.store.is_active(binding))
        binding["scope_id"] = "official"
        self.assertFalse(self.store.is_active(binding))

    def test_legacy_readonly_credentials_cannot_authenticate_v2(self):
        legacy = DeviceStore(self.root / "legacy-devices.json")
        issued = legacy.issue("old state only", "original-subject", "yaya", self.now)
        self.issue()
        self.assertIsNone(self.store.authenticate(issued["credential"]))
        fresh = self.issue()
        self.assertIsNone(legacy.authenticate(fresh["credential"]))

    def test_revocation_stops_existing_binding_and_token_immediately(self):
        issued = self.issue()
        binding = self.store.authenticate(issued["credential"])
        self.assertTrue(self.store.revoke(issued["device_id"]))
        self.assertFalse(self.store.is_active(binding))
        self.assertIsNone(self.store.authenticate(issued["credential"]))

    def test_corrupt_ledger_is_preserved_and_not_replaced_by_issuance(self):
        self.store.path.write_text("corrupt synthetic ledger", encoding="utf-8")
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.authenticate("denia2." + "a" * 43))
        with self.assertRaises(DeviceStoreError):
            self.issue()
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_permissions_cannot_include_management_and_chat_needs_turn_queries(self):
        for grants in (["admin:write"], ["chat:write"], ["chat:write", "chat:write", "turn:read"]):
            with self.subTest(grants=grants), self.assertRaises(ValueError):
                self.store.issue("synthetic", self.binding, grants, self.now)
        self.assertFalse(self.store.path.exists())

    def test_pairing_does_not_normalize_an_existing_subject_into_another_key(self):
        with self.assertRaises(ValueError):
            self.store.issue("synthetic", dict(self.binding, platform_subject_id=" original-subject "), ["state:read"], self.now)

    def test_device_listing_contains_no_credential_material(self):
        self.issue()
        listing = json.dumps(self.store.list_devices())
        self.assertNotIn("token_hash", listing)
        self.assertNotIn("denia2.", listing)
