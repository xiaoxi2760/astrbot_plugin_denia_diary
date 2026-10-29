"""Real loopback HTTP with generated test credentials and no models/QQ connection."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from core.pet_state.chat_devices import ChatDeviceStore
from core.pet_state.devices import DeviceStore
from pet_bridge.server import PetStateServer


class PetChatHTTPAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="denia-chat-http-")
        self.root = Path(self.temp.name)
        self.store = ChatDeviceStore(self.root / "v2.json")
        self.binding = {"scope_id": "official", "agent_id": "yaya", "platform_subject_id": "existing-subject", "preset_id": "existing-preset", "namespace": "short"}
        self.issued = self.store.issue("synthetic-http", self.binding, ["chat:write", "turn:read"], datetime.now(timezone.utc))
        self.headers = {"Authorization": "Bearer " + self.issued["credential"].encode().decode()}
        self.calls = []
        self.status = "completed"
        self.agent = "yaya"
        self.entered, self.release = None, None

        async def chat(binding, text, request_id):
            self.calls.append((binding, text, request_id))
            if self.entered is not None:
                self.entered.set()
                await self.release.wait()
            return self.record(request_id)

        self.server = PetStateServer(lambda _binding: {}, lambda _token: None, v2_callbacks={
            "authenticate": self.store.authenticate, "active": self.store.is_active,
            "capabilities": lambda binding: {"agent_id": binding["agent_id"], "capabilities": {"chat:write": True, "state:read": False}},
            "chat": chat, "query": lambda binding, request_id: self.record(request_id),
        })
        listener = await self.server.start("127.0.0.1", 0)
        self.assertTrue(listener["running"], listener)
        self.base = f"http://127.0.0.1:{listener['port']}/api/pet/v2"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.stop()
        self.temp.cleanup()

    def record(self, request_id):
        return {"request_id": request_id, "turn_id": "turn_synthetic", "status": self.status, "error_code": "",
                "result": {"request_id": request_id, "turn_id": "turn_synthetic", "agent_id": self.agent, "validated_reply": "窗边的位置给你留着。", "validation": "accepted", "degraded": False}}

    async def post(self, body=None, **kwargs):
        async with self.session.post(self.base + "/chat", headers=kwargs.pop("headers", self.headers), json=body if body is not None else {"request_id": "request_0001", "text": "一起看书吧。"}, **kwargs) as response:
            return response.status, await response.json(), response.headers

    async def test_valid_chat_has_bound_identity_and_no_store(self):
        async with self.session.get(self.base + "/capabilities", headers=self.headers) as response:
            capabilities = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(capabilities["device_id"], self.issued["device_id"])
        status, result, headers = await self.post()
        self.assertEqual(status, 200, result)
        self.assertEqual(result["validated_reply"], "窗边的位置给你留着。")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.calls[0][0]["platform_subject_id"], "existing-subject")
        self.assertNotIn("token_hash", json.dumps(result))

    async def test_legacy_credentials_cannot_enter_v2_and_v2_cannot_enter_v1(self):
        legacy = DeviceStore(self.root / "v1.json")
        issued = legacy.issue("state-only", "existing-subject", "yaya", datetime.now(timezone.utc))
        status, result, _ = await self.post(headers={"Authorization": "Bearer " + issued["credential"].encode().decode()})
        self.assertEqual(status, 401)
        async with self.session.get(self.base.replace("/v2", "/v1") + "/snapshot", headers=self.headers) as response:
            self.assertEqual(response.status, 401)
        self.assertFalse(self.calls)

    async def test_client_cannot_select_scope_subject_or_agent(self):
        for key in ("scope_id", "platform_subject_id", "agent_id"):
            status, _, _ = await self.post({"request_id": "request_0001", "text": "hello", key: "another"})
            self.assertEqual(status, 400)
        self.assertFalse(self.calls)

    async def test_origin_and_query_credentials_are_denied(self):
        status, _, _ = await self.post(headers={**self.headers, "Origin": "null"})
        self.assertEqual(status, 403)
        async with self.session.get(self.base + "/capabilities?credential=not-a-real-token", headers=self.headers) as response:
            self.assertEqual(response.status, 401)
        self.assertFalse(self.calls)

    async def test_revoke_during_callback_discards_the_completed_reply(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(self.post())
        await self.entered.wait()
        self.assertTrue(self.store.revoke(self.issued["device_id"]))
        self.release.set()
        status, result, _ = await pending
        self.assertEqual(status, 403)
        self.assertNotIn("窗边的位置", json.dumps(result, ensure_ascii=False))
        async with self.session.get(self.base + "/turns/request_0001", headers=self.headers) as response:
            self.assertEqual(response.status, 401)

    async def test_mismatched_backend_role_is_not_delivered(self):
        self.agent = "another-role"
        status, result, _ = await self.post()
        self.assertEqual(status, 403)
        self.assertNotIn("validated_reply", result)

    async def test_duplicate_keys_and_large_bodies_are_rejected_before_callback(self):
        async with self.session.post(self.base + "/chat", headers=self.headers, data=b'{"request_id":"request_0001","request_id":"request_0002","text":"hello"}') as response:
            self.assertEqual(response.status, 400)
        async with self.session.post(self.base + "/chat", headers=self.headers, data=b"x" * 17000) as response:
            self.assertEqual(response.status, 413)
        self.assertFalse(self.calls)

    async def test_pending_and_uncertain_have_explicit_non_delivery_status(self):
        self.status = "generating"
        status, result, _ = await self.post()
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "generating")
        self.assertNotIn("validated_reply", result)
        self.status = "uncertain"
        async with self.session.get(self.base + "/turns/request_0001", headers=self.headers) as response:
            result = await response.json()
            self.assertEqual(response.status, 409)
            self.assertEqual(result["status"], "uncertain")
            self.assertNotIn("validated_reply", result)

    async def test_per_device_burst_limit_is_bounded(self):
        statuses = []
        for _ in range(9):
            async with self.session.get(self.base + "/capabilities", headers=self.headers) as response:
                statuses.append(response.status)
        self.assertEqual(statuses, [200] * 8 + [429])
