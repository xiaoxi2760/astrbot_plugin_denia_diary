from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from support import TmpDirCase
from core.pet_state import DeviceStore
from pet_bridge.server import PetStateServer


class PetStateHTTPTests(TmpDirCase):
    def test_authenticated_read_only_http_contract_and_rate_limit(self) -> None:
        try:
            import aiohttp
        except ImportError:
            self.skipTest("aiohttp is not installed in this Python environment")

        async def run() -> None:
            snapshot = {
                "schema_version": 1,
                "agent_id": "yaya",
                "device_id": "device-test",
                "server_time": "2026-10-27T14:30:00+08:00",
                "timezone": "Asia/Shanghai",
                "ttl_seconds": 60,
                "valid_until": "2026-10-27T14:31:00+08:00",
                "refresh_after_seconds": 15,
                "life": {"enabled": True, "status": "empty", "date": "2026-10-27", "revision": 1, "current": None, "next": None},
                "mood": {"status": "empty", "word": None, "observed_at": None, "valid_until": None},
                "presentation": {"expression": "neutral"},
            }
            bad_snapshot = False

            def authenticate(credential: str) -> dict | None:
                if credential != "test-device-credential":
                    return None
                return {"device_id": "device-test", "agent_id": "yaya", "principal_id": "owner-1"}

            def project(binding: dict) -> dict:
                if bad_snapshot:
                    raise RuntimeError("private failure detail")
                return snapshot

            server = PetStateServer(project, authenticate)
            started = await server.start("127.0.0.1", 0)
            self.assertTrue(started["running"])
            self.assertEqual(started["host"], "127.0.0.1")
            self.assertGreater(started["port"], 0)
            self.assertIsNone(started["error"])
            duplicate = await server.start("127.0.0.1", 0)
            self.assertEqual(duplicate["port"], started["port"])

            base = f"http://127.0.0.1:{started['port']}"
            async with aiohttp.ClientSession() as client:
                headers = {"Authorization": "Bearer test-device-credential"}
                response = await client.get(base + "/api/pet/v1/snapshot", headers=headers)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)
                self.assertTrue(response.headers.get("Content-Type", "").startswith("application/json"))
                self.assertEqual(await response.json(), snapshot)

                limited = await client.get(base + "/api/pet/v1/snapshot", headers=headers)
                self.assertEqual(limited.status, 429)
                self.assertEqual(limited.headers.get("Retry-After"), "1")
                self.assertEqual((await limited.json())["error"]["code"], "rate_limited")

                await asyncio.sleep(1.05)
                bad_header = await client.get(
                    base + "/api/pet/v1/snapshot", headers={"Authorization": "Basic secret"}
                )
                self.assertEqual(bad_header.status, 401)
                self.assertEqual((await bad_header.json())["error"]["code"], "unauthorized")

                query_auth = await client.get(
                    base + "/api/pet/v1/snapshot?token=test-device-credential", headers=headers
                )
                self.assertEqual(query_auth.status, 401)
                origin = await client.get(
                    base + "/api/pet/v1/snapshot",
                    headers={**headers, "Origin": "https://pet.example"},
                )
                self.assertEqual(origin.status, 403)
                wrong_binding = await client.get(
                    base + "/api/pet/v1/snapshot", headers={"Authorization": "Bearer wrong"}
                )
                self.assertEqual(wrong_binding.status, 401)
                method = await client.post(base + "/api/pet/v1/snapshot", headers=headers)
                self.assertEqual(method.status, 405)
                missing = await client.get(base + "/api/pet/v1/other", headers=headers)
                self.assertEqual(missing.status, 404)

                bad_snapshot = True
                await asyncio.sleep(1.05)
                callback_error = await client.get(base + "/api/pet/v1/snapshot", headers=headers)
                self.assertEqual(callback_error.status, 503)
                body = await callback_error.text()
                self.assertNotIn("private failure detail", body)
                self.assertEqual(json.loads(body)["error"]["code"], "unavailable")

            stopped = await server.stop()
            self.assertFalse(stopped["running"])
            self.assertIsNone(stopped["host"])

        asyncio.run(run())

    def test_invalid_credentials_are_limited_before_device_ledger_reads(self) -> None:
        try:
            import aiohttp
        except ImportError:
            self.skipTest("aiohttp is not installed in this Python environment")

        class CountingDeviceStore(DeviceStore):
            def __init__(self, path) -> None:
                super().__init__(path)
                self.read_count = 0

            def _read(self):
                self.read_count += 1
                return super()._read()

        async def run() -> None:
            store = CountingDeviceStore(self.root / "pet-devices.json")
            store.issue("paired device", "owner-1", "yaya", datetime(2026, 1, 1, tzinfo=timezone.utc))
            store.read_count = 0
            server = PetStateServer(lambda _binding: {}, store.authenticate)
            started = await server.start("127.0.0.1", 0)
            url = f"http://127.0.0.1:{started['port']}/api/pet/v1/snapshot"

            clock = [10.0]
            try:
                with patch("pet_bridge.server.monotonic", side_effect=lambda: clock[0]):
                    async with aiohttp.ClientSession() as client:
                        async def denied(index: int) -> int:
                            response = await client.get(
                                url,
                                headers={
                                    "Authorization": "Bearer invalid-credential",
                                    "X-Forwarded-For": f"192.0.2.{index % 250 + 1}",
                                },
                            )
                            await response.read()
                            return response.status

                        statuses = await asyncio.gather(*(denied(index) for index in range(100)))
                        self.assertEqual(statuses.count(401), 32)
                        self.assertEqual(statuses.count(429), 68)
                        self.assertEqual(store.read_count, 32)
                        self.assertIn("127.0.0.1", server._source_requests)
                        self.assertNotIn("192.0.2.1", server._source_requests)
                        self.assertLessEqual(len(server._source_requests), 128)
                        self.assertLessEqual(len(server._global_requests), 64)

                        clock[0] = 11.01
                        recovered = await client.get(
                            url, headers={"Authorization": "Bearer invalid-credential"}
                        )
                        self.assertEqual(recovered.status, 401)
                        await recovered.read()
                        self.assertEqual(store.read_count, 33)

                        # Synthetic peers exercise the memory bound independently
                        # of the single loopback source used by this HTTP test.
                        for index in range(200):
                            server._check_request_rate(f"198.51.100.{index}")
                        self.assertLessEqual(len(server._source_requests), 128)
            finally:
                stopped = await server.stop()
                self.assertFalse(stopped["running"])
                self.assertFalse(server._source_requests)
                self.assertFalse(server._global_requests)

        asyncio.run(run())

    def test_bad_snapshot_binding_and_failed_bind_are_cleaned_up(self) -> None:
        try:
            import aiohttp
        except ImportError:
            self.skipTest("aiohttp is not installed in this Python environment")

        async def run() -> None:
            binding = {"device_id": "device-test", "agent_id": "yaya", "principal_id": "owner-1"}
            base_snapshot = {"schema_version": 1, "agent_id": "wrong", "device_id": "device-test"}

            server = PetStateServer(lambda _binding: base_snapshot, lambda _token: binding)
            running = await server.start("127.0.0.1", 0)
            contender = PetStateServer(lambda _binding: base_snapshot, lambda _token: binding)
            failed = await contender.start("127.0.0.1", running["port"])
            self.assertFalse(failed["running"])
            self.assertEqual(failed["error"], "listener unavailable")
            self.assertIsNone(failed["host"])
            self.assertIsNone(failed["port"])

            async with aiohttp.ClientSession() as client:
                denied = await client.get(
                    f"http://127.0.0.1:{running['port']}/api/pet/v1/snapshot",
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(denied.status, 403)
                self.assertEqual((await denied.json())["error"]["code"], "forbidden")
            await server.stop()

            restarted = await contender.start("127.0.0.1", running["port"])
            self.assertTrue(restarted["running"])
            self.assertEqual(restarted["port"], running["port"])
            await contender.stop()

            def denied_auth(_token: str) -> None:
                raise PermissionError("private binding detail")

            auth_denied = PetStateServer(lambda _binding: base_snapshot, denied_auth)
            auth_status = await auth_denied.start("127.0.0.1", 0)
            async with aiohttp.ClientSession() as client:
                denied = await client.get(
                    f"http://127.0.0.1:{auth_status['port']}/api/pet/v1/snapshot",
                    headers={"Authorization": "Bearer token"},
                )
                self.assertEqual(denied.status, 403)
                self.assertNotIn("private binding detail", await denied.text())
            await auth_denied.stop()

        asyncio.run(run())

    def test_non_json_numbers_and_oversized_snapshots_are_rejected(self) -> None:
        try:
            import aiohttp
        except ImportError:
            self.skipTest("aiohttp is not installed in this Python environment")

        async def run() -> None:
            mode = {"value": float("nan")}
            binding = {"device_id": "device-test", "agent_id": "yaya", "principal_id": "owner-1"}

            def project(_binding: dict) -> dict:
                return {"schema_version": 1, "agent_id": "yaya", "device_id": "device-test", **mode}

            server = PetStateServer(project, lambda _token: binding)
            running = await server.start("127.0.0.1", 0)
            url = f"http://127.0.0.1:{running['port']}/api/pet/v1/snapshot"
            async with aiohttp.ClientSession() as client:
                headers = {"Authorization": "Bearer token"}
                invalid_number = await client.get(url, headers=headers)
                self.assertEqual(invalid_number.status, 503)
                self.assertEqual((await invalid_number.json())["error"]["code"], "unavailable")

                await asyncio.sleep(1.05)
                mode.clear()
                mode["payload"] = "x" * 32769
                oversized = await client.get(url, headers=headers)
                self.assertEqual(oversized.status, 503)
                self.assertEqual((await oversized.json())["error"]["code"], "unavailable")
            await server.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
