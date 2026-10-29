from __future__ import annotations

import hashlib
import json
import os
import stat
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from support import TmpDirCase
from core.pet_state import BindingMismatchError, DeviceStore, build_snapshot
from core.pet_state.errors import DeviceStoreError, DeviceStoreWriteError

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 27, 14, 30, tzinfo=TZ)
FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "pet-state-v1"


def _settings(*, enabled: bool = True, agent_id: str = "yaya") -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        timezone="Asia/Shanghai",
        pet_bridge={"enabled": False, "host": "127.0.0.1", "port": 6196, "agent_id": agent_id},
        love_peers=["owner-1"],
        zone=lambda: TZ,
    )


def _binding(*, agent_id: str = "yaya", device_id: str = "device-fixture") -> dict[str, str]:
    return {"device_id": device_id, "agent_id": agent_id, "principal_id": "owner-1"}


def _activity(
    identifier: str,
    start: str,
    end: str,
    status: str,
    location: str = "",
    title: str = "活动",
) -> dict[str, str]:
    return {
        "id": identifier,
        "start": start,
        "end": end,
        "title": title,
        "location": location,
        "status": status,
        "outcome": "内部额外字段不得输出",
    }


class _Life:
    def __init__(self, data: dict | None = None, error: Exception | None = None) -> None:
        self.data = data or {}
        self.error = error
        self.seen_now = None

    def snapshot(self, *, now: datetime) -> dict:
        self.seen_now = now
        if self.error:
            raise self.error
        return self.data


class _State:
    def __init__(self, mood: dict) -> None:
        self.mood = mood
        self.seen_now = None

    def public_mood(self, *, now: datetime) -> dict:
        self.seen_now = now
        return self.mood


class DeviceStoreTests(TmpDirCase):
    def test_issue_hashes_credential_and_binding_survives_restart(self) -> None:
        path = self.root / "pet-devices.json"
        store = DeviceStore(path)
        issued = store.issue("Denia PC", "owner-1", "yaya", NOW)
        credential = issued["credential"]

        self.assertEqual(issued["agent_id"], "yaya")
        self.assertNotIn(credential, repr(issued))
        self.assertNotIn(credential, str(issued))
        self.assertIn(credential, json.dumps(issued))
        expanded_response = {**issued, "ok": True}
        self.assertNotIn(credential, repr(expanded_response))
        self.assertIn(credential, json.dumps(expanded_response))
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn(credential, raw)
        self.assertIn(hashlib.sha256(credential.encode()).hexdigest(), raw)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        binding = DeviceStore(path).authenticate(credential)
        self.assertEqual(
            binding,
            {"device_id": issued["device_id"], "agent_id": "yaya", "principal_id": "owner-1"},
        )
        listing = DeviceStore(path).list_devices()
        self.assertEqual(len(listing), 1)
        self.assertNotIn("credential", listing[0])
        self.assertNotIn("token_hash", listing[0])
        self.assertEqual(listing[0]["principal_id"], "owner-1")

    def test_revoke_is_immediate_persistent_and_frees_a_slot(self) -> None:
        path = self.root / "pet-devices.json"
        first = DeviceStore(path).issue("one", "owner-1", "yaya", NOW)
        store = DeviceStore(path)
        self.assertTrue(store.revoke(first["device_id"]))
        self.assertIsNone(DeviceStore(path).authenticate(first["credential"]))
        self.assertFalse(store.revoke(first["device_id"]))

        second = DeviceStore(path).issue("two", "owner-1", "yaya", NOW)
        self.assertIsNotNone(DeviceStore(path).authenticate(second["credential"]))

    def test_sixteen_active_device_limit(self) -> None:
        store = DeviceStore(self.root / "pet-devices.json")
        issued = [store.issue(f"device-{index}", "owner-1", "yaya", NOW) for index in range(16)]
        self.assertEqual(len(store.list_devices()), 16)
        with self.assertRaises(DeviceStoreError):
            store.issue("seventeenth", "owner-1", "yaya", NOW)
        self.assertTrue(store.revoke(issued[0]["device_id"]))
        self.assertEqual(len(store.list_devices()), 15)
        store.issue("replacement", "owner-1", "yaya", NOW)
        self.assertEqual(len(store.list_devices()), 16)

    def test_bad_json_utf8_or_schema_fails_closed_without_rewriting(self) -> None:
        path = self.root / "pet-devices.json"
        for invalid in (
            b"{broken",
            b"\xff",
            b'{"schema":2,"devices":[]}',
            b'{"schema":1,"devices":[],"unexpected":true}',
            b" " * (64 * 1024 + 1),
        ):
            with self.subTest(invalid=invalid):
                path.write_bytes(invalid)
                store = DeviceStore(path)
                with self.assertRaises(DeviceStoreError):
                    store.issue("device", "owner-1", "yaya", NOW)
                self.assertIsNone(store.authenticate("unrecognized"))
                self.assertFalse(store.revoke("any-device"))
                with self.assertRaises(DeviceStoreError):
                    store.list_devices()
                self.assertEqual(path.read_bytes(), invalid)

    def test_write_failure_does_not_return_an_issued_device(self) -> None:
        path = self.root / "pet-devices.json"
        store = DeviceStore(path)
        with patch.object(store, "_write", side_effect=DeviceStoreWriteError("write failed")):
            with self.assertRaises(DeviceStoreWriteError):
                store.issue("device", "owner-1", "yaya", NOW)
        self.assertFalse(path.exists())


class SnapshotTests(TmpDirCase):
    def test_ready_projection_matches_frozen_fixture_and_uses_one_instant(self) -> None:
        fixture = json.loads((FIXTURES / "example-ready.json").read_text(encoding="utf-8"))
        activities = [
            _activity("reading", "14:00", "15:00", "current", "窗边", "看书"),
            _activity("walk", "15:00", "16:00", "planned", title="散步"),
        ]
        life = _Life(
            {
                "ok": True,
                "enabled": True,
                "date": "2026-10-27",
                "timezone": "Asia/Shanghai",
                "revision": 3,
                "activities": activities,
                "current": activities[0],
                "next": activities[1],
                "generation": {"status": "ready", "error": "private"},
                "recent": [{"title": "must not escape"}],
            }
        )
        state = _State(
            {
                "status": "fresh",
                "word": "开心",
                "observed_at": "2026-10-27T13:00:00+08:00",
                "valid_until": "2026-10-27T19:00:00+08:00",
                "valence": 0.9,
                "private_note": "must not escape",
            }
        )

        result = build_snapshot(life, state, _settings(), _binding(), NOW)

        self.assertEqual(result, fixture)
        self.assertIs(life.seen_now, NOW)
        self.assertIs(state.seen_now, NOW)
        self.assertNotIn("recent", json.dumps(result))
        self.assertNotIn("generation", json.dumps(result))
        self.assertNotIn("valence", json.dumps(result))
        self.assertNotIn("owner-1", json.dumps(result))

    def test_end_at_24_00_becomes_next_day_and_refresh_tracks_boundary(self) -> None:
        now = datetime(2026, 10, 27, 23, 59, tzinfo=TZ)
        activity = _activity("late", "23:30", "24:00", "current")
        life = _Life(
            {
                "ok": True,
                "enabled": True,
                "date": "2026-10-27",
                "timezone": "Asia/Shanghai",
                "revision": 7,
                "activities": [activity],
                "current": activity,
                "next": None,
                "generation": {"status": "ready"},
            }
        )
        result = build_snapshot(
            life,
            _State({"status": "empty", "word": None, "observed_at": None, "valid_until": None}),
            _settings(),
            _binding(),
            now,
        )
        self.assertEqual(result["life"]["current"]["end_at"], "2026-10-28T00:00:00+08:00")
        self.assertEqual(result["life"]["status"], "ready")
        self.assertGreaterEqual(result["refresh_after_seconds"], 1)
        self.assertLessEqual(result["refresh_after_seconds"], 15)

    def test_refresh_moves_ahead_of_activity_and_mood_expiry(self) -> None:
        current_now = datetime(2026, 10, 27, 14, 29, 58, tzinfo=TZ)
        current = _activity("short", "14:00", "14:30", "current")
        life_data = {
            "ok": True,
            "enabled": True,
            "date": "2026-10-27",
            "timezone": "Asia/Shanghai",
            "revision": 9,
            "activities": [current],
            "current": current,
            "next": None,
            "generation": {"status": "ready"},
        }
        mood = {
            "status": "fresh",
            "word": "安心",
            "observed_at": "2026-10-27T14:00:00+08:00",
            "valid_until": "2026-10-27T14:30:10+08:00",
        }
        activity_result = build_snapshot(_Life(life_data), _State(mood), _settings(), _binding(), current_now)
        self.assertEqual(activity_result["refresh_after_seconds"], 2)

        no_activity = {**life_data, "activities": [], "current": None}
        mood_result = build_snapshot(_Life(no_activity), _State(mood), _settings(), _binding(), NOW)
        self.assertEqual(mood_result["refresh_after_seconds"], 10)

    def test_refresh_rounds_up_near_activity_or_mood_boundaries(self) -> None:
        no_plan = {
            "ok": True,
            "enabled": True,
            "date": "2026-10-27",
            "timezone": "Asia/Shanghai",
            "revision": 1,
            "activities": [],
            "current": None,
            "next": None,
            "generation": {"status": "idle"},
        }
        for expiry, expected in (
            ("2026-10-27T14:30:00.200000+08:00", 1),
            ("2026-10-27T14:30:01+08:00", 1),
            ("2026-10-27T14:30:01.200000+08:00", 2),
        ):
            with self.subTest(expiry=expiry):
                mood = {
                    "status": "fresh",
                    "word": "平静",
                    "observed_at": "2026-10-27T14:00:00+08:00",
                    "valid_until": expiry,
                }
                result = build_snapshot(
                    _Life(no_plan), _State(mood), _settings(), _binding(), NOW
                )
                self.assertEqual(result["refresh_after_seconds"], expected)

    def test_life_status_rules_and_mood_staleness_projection(self) -> None:
        empty_mood = {"status": "empty", "word": None, "observed_at": None, "valid_until": None}

        def project(life_data: dict, mood: dict = empty_mood, *, enabled: bool = True) -> dict:
            return build_snapshot(_Life(life_data), _State(mood), _settings(enabled=enabled), _binding(), NOW)

        base = {"ok": True, "enabled": True, "date": "2026-10-27", "timezone": "Asia/Shanghai", "revision": 4, "activities": [], "current": None, "next": None, "generation": {"status": "idle"}}
        self.assertEqual(project(base)["life"]["status"], "empty")
        self.assertEqual(project({**base, "generation": {"status": "generating"}})["life"]["status"], "planning")
        self.assertEqual(project({**base, "activities": [_activity("past", "08:00", "09:00", "elapsed")]})["life"]["status"], "ready")
        self.assertEqual(project({**base, "ok": False, "revision": 99})["life"]["revision"], None)
        disabled = project({**base, "enabled": False, "current": _activity("x", "09:00", "10:00", "current")})
        self.assertEqual(disabled["life"]["status"], "disabled")
        self.assertIsNone(disabled["life"]["current"])
        self.assertIsNone(disabled["life"]["next"])

        stale = project(
            base,
            {"status": "stale", "word": None, "observed_at": "2026-10-27T07:00:00+08:00", "valid_until": "2026-10-27T13:00:00+08:00"},
        )
        self.assertEqual(stale["mood"]["status"], "stale")
        self.assertIsNone(stale["mood"]["word"])

    def test_unavailable_life_and_wrong_agent_are_safe(self) -> None:
        state = _State({"status": "unavailable", "word": None, "observed_at": None, "valid_until": None})
        result = build_snapshot(
            _Life(error=OSError("private file path")), state, _settings(), _binding(), NOW
        )
        self.assertEqual(result["life"]["status"], "unavailable")
        self.assertIsNone(result["life"]["revision"])
        self.assertIsNone(result["life"]["current"])
        self.assertNotIn("private file path", json.dumps(result))
        with self.assertRaises(BindingMismatchError):
            build_snapshot(_Life(), state, _settings(), _binding(agent_id="other"), NOW)
        with self.assertRaises(BindingMismatchError):
            build_snapshot(
                _Life(), state, _settings(), {**_binding(), "principal_id": "other-owner"}, NOW
            )

    def test_invalid_life_activity_and_fresh_mood_dates_fail_closed(self) -> None:
        valid = {
            "ok": True,
            "enabled": True,
            "date": "2026-10-27",
            "timezone": "Asia/Shanghai",
            "revision": 8,
            "activities": [],
            "current": None,
            "next": None,
            "generation": {"status": "idle"},
        }
        malformed_slots = [
            {"current": _activity("wrong-current", "14:00", "15:00", "planned")},
            {"next": _activity("wrong-next", "15:00", "16:00", "current")},
            {"date": "not-a-date"},
            {"timezone": "UTC"},
            {"date": "2026-10-28"},
            {"current": _activity("bad-time", "25:00", "26:00", "current")},
        ]
        for mutation in malformed_slots:
            with self.subTest(mutation=mutation):
                result = build_snapshot(
                    _Life({**valid, **mutation}),
                    _State({"status": "empty", "word": None, "observed_at": None, "valid_until": None}),
                    _settings(),
                    _binding(),
                    NOW,
                )
                self.assertEqual(result["life"]["status"], "unavailable")
                self.assertIsNone(result["life"]["revision"])
                self.assertIsNone(result["life"]["current"])
                self.assertIsNone(result["life"]["next"])

        invalid_mood = {
            "status": "fresh",
            "word": "开心",
            "observed_at": "2026-10-27T13:00:00",
            "valid_until": "2026-10-27T19:00:00+08:00",
        }
        result = build_snapshot(_Life(valid), _State(invalid_mood), _settings(), _binding(), NOW)
        self.assertEqual(
            result["mood"],
            {"status": "unavailable", "word": None, "observed_at": None, "valid_until": None},
        )


if __name__ == "__main__":
    unittest.main()
