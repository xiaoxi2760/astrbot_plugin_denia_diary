"""角色公开日常计划。该模块只管理虚拟角色的日程，不执行现实操作。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time as monotonic_time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import scope, storage

_SCHEMA = 1
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_MAX_ACTIVITIES = 12
_MAX_RECENT = 5


@dataclass
class Life:
    """读取、生成和维护角色的每日虚拟日常。"""

    settings: Any
    layout: Any
    locks: Any = None

    def __post_init__(self) -> None:
        if self.locks is None:
            self.locks = storage.KeyedLocks()

    @property
    def _path(self) -> Path:
        return Path(self.layout.life)

    def _lock(self) -> asyncio.Lock:
        return self.locks.lock_for(self._path)

    def _zone(self) -> ZoneInfo:
        zone_method = getattr(self.settings, "zone", None)
        zone = zone_method() if callable(zone_method) else None
        if isinstance(zone, ZoneInfo):
            return zone
        key = getattr(zone, "key", None) or getattr(self.settings, "timezone", None)
        if not key:
            key = "Asia/Shanghai"
        return ZoneInfo(str(key))

    def _now(self, now: datetime, zone: ZoneInfo) -> datetime:
        if not isinstance(now, datetime):
            raise TypeError("now 必须是 datetime")
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=zone)
        return now.astimezone(zone)

    def _final_now(self, supplied: datetime, zone: ZoneInfo, *, elapsed: float) -> datetime:
        """Advance the injected instant by real elapsed time, including across midnight."""
        initial_utc = self._now(supplied, zone).astimezone(timezone.utc)
        return (initial_utc + timedelta(seconds=max(0.0, elapsed))).astimezone(zone)

    def _enabled(self) -> bool:
        life = getattr(self.settings, "life", None)
        return bool(getattr(self.settings, "enabled", True)) and bool(
            _get(life, "enabled", False)
        )

    def _life_config(self) -> Any:
        return getattr(self.settings, "life", None)

    def _config_int(self, name: str, default: int, minimum: int = 1) -> int:
        try:
            value = int(_get(self._life_config(), name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _routine(self) -> Any:
        raw = getattr(self.settings, "state", None)
        if isinstance(raw, str):
            return raw[:500]
        if isinstance(raw, Mapping):
            source = raw
        elif raw is not None and hasattr(raw, "__dict__"):
            source = vars(raw)
        else:
            return None
        allowed = (
            "wake", "sleep", "wake_time", "sleep_time", "routine", "daily_routine",
            "rhythm", "schedule", "work_start", "work_end", "rest", "lunch",
            "作息", "起床", "睡觉", "工作", "午休", "休息",
        )
        result = {
            str(key): value
            for key, value in source.items()
            if any(token in str(key).lower() for token in allowed)
        }
        return result or None

    def _settings_fingerprint(self) -> str:
        life = self._life_config()
        values = {
            "enabled": self._enabled(),
            "provider_id": _get(life, "provider_id", ""),
            "character": str(_get(life, "character", "") or "")[:1200],
            "routine": self._routine(),
            "timezone": self._zone().key,
            "timeout": self._config_int("timeout_seconds", 30),
            "retry": self._config_int("retry_minutes", 30),
            "max_attempts": self._config_int("max_attempts", 3),
        }
        return json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)

    def _read_data(self) -> tuple[dict[str, Any] | None, str]:
        path = self._path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"schema": _SCHEMA, "revision": 0, "days": {}}, ""
        except OSError as exc:
            return None, f"无法读取日常计划：{exc}"
        try:
            data = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            return None, f"日常计划 JSON 损坏：{exc}"
        if not isinstance(data, dict):
            return None, "日常计划顶层必须是对象"
        version = data.get("schema")
        if not isinstance(version, int) or isinstance(version, bool):
            return None, "日常计划 schema 无效"
        if version > _SCHEMA:
            return None, f"日常计划 schema {version} 高于当前支持版本"
        if version != _SCHEMA:
            return None, f"不支持日常计划 schema {version}"
        file_version = data.get("schema_version", storage.SCHEMA_VERSION)
        if not isinstance(file_version, int) or isinstance(file_version, bool):
            return None, "日常计划 schema_version 无效"
        if file_version > storage.SCHEMA_VERSION:
            return None, f"日常计划 schema_version {file_version} 高于当前支持版本"
        revision = data.get("revision")
        days = data.get("days")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            return None, "日常计划 revision 无效"
        if not isinstance(days, dict):
            return None, "日常计划 days 必须是对象"
        for key, day in days.items():
            error = _validate_day_data(key, day)
            if error:
                return None, error
        return data, ""

    def _write_data(self, data: dict[str, Any]) -> None:
        storage.atomic_write_json(self._path, data)

    def snapshot(self, *, now: datetime) -> dict[str, Any]:
        zone = self._zone()
        local_now = self._now(now, zone)
        date_text = local_now.date().isoformat()
        tz_name = zone.key
        key = _day_key(date_text, tz_name)
        data, read_error = self._read_data()
        if data is None:
            data = {"schema": _SCHEMA, "revision": 0, "days": {}}
        return self._make_snapshot(
            data,
            key=key,
            date_text=date_text,
            tz_name=tz_name,
            now=local_now,
            read_error=read_error,
        )

    def _make_snapshot(
        self,
        data: dict[str, Any],
        *,
        key: str,
        date_text: str,
        tz_name: str,
        now: datetime,
        read_error: str = "",
    ) -> dict[str, Any]:
        day = data.get("days", {}).get(key) if isinstance(data.get("days"), dict) else None
        generation = _generation_view(day.get("generation") if isinstance(day, dict) else None)
        generation["max_attempts"] = self._config_int("max_attempts", 3)
        enabled = self._enabled()
        if not enabled:
            generation["status"] = "disabled"
        if read_error:
            generation.update(status="failed", error=read_error, next_retry_at=None)

        raw_activities = day.get("activities", []) if isinstance(day, dict) else []
        activities = [
            _public_activity(activity, date_text, now)
            for activity in raw_activities
        ]
        activities.sort(key=lambda item: item["start"])
        current = next((item for item in activities if item["status"] == "current"), None)
        upcoming = [
            item for item in activities
            if item["status"] == "planned" and item["start"] > now.strftime("%H:%M")
        ]
        next_activity = upcoming[0] if upcoming else None

        confirmed: list[dict[str, Any]] = []
        all_days = data.get("days", {}) if isinstance(data.get("days"), dict) else {}
        for stored_day in all_days.values():
            if not isinstance(stored_day, dict):
                continue
            stored_date = str(stored_day.get("date") or "")
            if stored_date > date_text:
                continue
            for activity in stored_day.get("activities", []):
                if activity.get("status") not in {"done", "skipped"}:
                    continue
                item = _public_activity(activity, stored_date, now, include_date=True)
                confirmed.append(item)
        confirmed.sort(key=lambda item: (item.get("date", ""), item["end"]), reverse=True)
        recent = confirmed[:_MAX_RECENT] if enabled and not read_error else []

        return {
            "ok": not bool(read_error),
            "enabled": enabled,
            "date": date_text,
            "timezone": tz_name,
            "revision": int(data.get("revision", 0)),
            "activities": activities,
            "current": current,
            "next": next_activity,
            "recent": recent,
            "generation": generation,
            "updated_at": day.get("updated_at") if isinstance(day, dict) else None,
        }

    def _response(self, ok: bool, now: datetime, *, error: str = "", conflict: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": ok}
        if error:
            result["error"] = error
        if conflict:
            result["conflict"] = True
        result["snapshot"] = self.snapshot(now=now)
        return result

    async def tick(
        self,
        planner: Callable[[str], Any],
        *,
        now: datetime,
        force: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        zone = self._zone()
        local_now = self._now(now, zone)
        date_text = local_now.date().isoformat()
        tz_name = zone.key
        key = _day_key(date_text, tz_name)
        if not self._enabled():
            return self._response(False, local_now, error="日常计划已禁用")
        if not callable(planner):
            return self._response(False, local_now, error="日常规划器不可用")

        max_attempts = self._config_int("max_attempts", 3)
        retry_minutes = self._config_int("retry_minutes", 30)
        timeout_seconds = self._config_int("timeout_seconds", 30)
        settings_fingerprint = self._settings_fingerprint()
        token = uuid.uuid4().hex
        claim_revision: int | None = None
        prompt = ""
        started_monotonic = 0.0
        async with self._lock():
            data, error = self._read_data()
            if data is None:
                return self._response(False, local_now, error=error)
            if expected_revision is not None and expected_revision != data["revision"]:
                return self._response(False, local_now, error="计划版本已变化，请刷新后重试", conflict=True)
            day = data["days"].get(key)
            if day is None:
                day = _new_day(date_text, tz_name)
                data["days"][key] = day
            generation = day["generation"]
            generation = _normalize_generation(generation)
            day["generation"] = generation
            if generation.get("status") == "generating":
                lease_until = _parse_iso(generation.get("lease_until"))
                if lease_until is not None and lease_until > local_now:
                    return self._response(False, local_now, error="日常计划正在生成")
            if not force and (generation.get("status") == "ready" or day.get("activities")):
                return self._response(True, local_now)
            next_retry = _parse_iso(generation.get("next_retry_at"))
            if next_retry is not None and next_retry > local_now:
                return self._response(False, local_now, error="日常计划仍在重试等待时间内")
            if int(generation.get("attempts", 0)) >= max_attempts:
                exhausted_error = generation.get("error") or "今天的规划尝试次数已用完"
                if generation.get("status") == "generating":
                    generation.update(
                        status="failed",
                        error=exhausted_error,
                        next_retry_at=None,
                        lease_until=None,
                        token=None,
                    )
                    data["revision"] += 1
                    day["updated_at"] = local_now.isoformat()
                    self._write_data(data)
                return self._response(False, local_now, error=exhausted_error)

            generation.update(
                status="generating",
                error="",
                attempts=int(generation.get("attempts", 0)) + 1,
                next_retry_at=None,
                lease_until=(local_now + timedelta(seconds=timeout_seconds + 10)).isoformat(),
                token=token,
            )
            data["revision"] += 1
            claim_revision = data["revision"]
            day["updated_at"] = local_now.isoformat()
            _prune_days(data["days"], keep_key=key)
            self._write_data(data)
            prompt = self._make_prompt(date_text, tz_name)
            started_monotonic = monotonic_time.monotonic()

        try:
            response_text = await asyncio.wait_for(planner(prompt), timeout=timeout_seconds)
            planned = _parse_plan(response_text, date_text, tz_name)
        except asyncio.CancelledError:
            await self._finish_failure(
                key, token, local_now, "日常规划已取消", retry_minutes, max_attempts,
                claim_revision=claim_revision,
            )
            raise
        except asyncio.TimeoutError:
            return await self._finish_failure(
                key, token, local_now, "日常规划超时", retry_minutes, max_attempts,
                claim_revision=claim_revision,
            )
        except Exception as exc:  # noqa: BLE001 - provider and response errors become bounded retry state
            message = str(exc).strip() or type(exc).__name__
            return await self._finish_failure(
                key, token, local_now, message[:240], retry_minutes, max_attempts,
                claim_revision=claim_revision,
            )

        async with self._lock():
            data, error = self._read_data()
            if data is None:
                return self._response(False, local_now, error=error)
            day = data["days"].get(key)
            if not isinstance(day, dict) or day.get("generation", {}).get("token") != token:
                return self._response(False, local_now, error="计划已在生成期间更新", conflict=True)
            final_zone = self._zone()
            final_now = self._final_now(
                now, final_zone, elapsed=monotonic_time.monotonic() - started_monotonic
            )
            final_key = _day_key(final_now.date().isoformat(), final_zone.key)
            if (
                data["revision"] != claim_revision
                or not self._enabled()
                or final_key != key
                or self._settings_fingerprint() != settings_fingerprint
            ):
                self._clear_generation(day, ready=bool(day.get("activities")))
                data["revision"] += 1
                day["updated_at"] = final_now.isoformat()
                self._write_data(data)
                return self._response(False, final_now, error="配置或计划已变化，本次结果未应用", conflict=True)

            existing = list(day.get("activities", []))
            if force:
                protected = [
                    activity for activity in existing
                    if activity.get("status") in {"done", "skipped"}
                    or _activity_start(activity, date_text, final_zone) <= final_now
                ]
                protected_ids = {activity.get("id") for activity in protected}
                future = [
                    activity for activity in planned
                    if _activity_start(activity, date_text, final_zone) > final_now
                    and activity.get("id") not in protected_ids
                ]
                merged = protected + future
                merged.sort(key=lambda item: item["start"])
                overlap = "保留已开始活动后，一天最多安排 12 项" if len(merged) > _MAX_ACTIVITIES else _validate_overlaps(merged)
                if overlap:
                    return await self._fail_locked(
                        data, day, key, token, final_now, overlap,
                        retry_minutes, max_attempts,
                    )
                planned = merged
            day["activities"] = planned
            generation = _normalize_generation(day.get("generation"))
            generation.update(status="ready", error="", next_retry_at=None, lease_until=None, token=None)
            day["generation"] = generation
            day["updated_at"] = final_now.isoformat()
            data["revision"] += 1
            _prune_days(data["days"], keep_key=key)
            self._write_data(data)
            return self._response(True, final_now)

    def _make_prompt(self, date_text: str, tz_name: str) -> str:
        character = str(_get(self._life_config(), "character", "") or "").strip()[:1200]
        routine = self._routine()
        return (
            "请为虚拟角色安排一天的公开日常，只描述角色自己的虚构活动，不是用户日历，"
            "也不表示现实中的用户或角色已执行任何操作。不要使用私人记忆或对话记录。\n"
            f"日期：{date_text}\n时区：{tz_name}\n角色设定：{character or '自然、平和地生活'}\n"
            f"作息设定：{json.dumps(routine, ensure_ascii=False, default=str) if routine else '未提供'}\n"
            "返回一个 JSON 对象：date 为上述日期，activities 为 1 到 12 项。每项只含 start、end、title、location；"
            "时间用严格 HH:MM，end 可为 24:00；标题和地点各不超过 60 字。活动必须顺序合理且不重叠。"
        )

    async def _finish_failure(
        self,
        key: str,
        token: str,
        now: datetime,
        error: str,
        retry_minutes: int,
        max_attempts: int,
        *,
        claim_revision: int | None,
    ) -> dict[str, Any]:
        async with self._lock():
            data, read_error = self._read_data()
            if data is None:
                return self._response(False, now, error=read_error)
            day = data["days"].get(key)
            if not isinstance(day, dict) or day.get("generation", {}).get("token") != token:
                return self._response(False, now, error="计划已在生成期间更新", conflict=True)
            if claim_revision is not None and data["revision"] != claim_revision:
                self._clear_generation(day, ready=bool(day.get("activities")))
                data["revision"] += 1
                day["updated_at"] = now.isoformat()
                self._write_data(data)
                return self._response(False, now, error="计划已在生成期间更新", conflict=True)
            return await self._fail_locked(
                data, day, key, token, now, error, retry_minutes, max_attempts,
            )

    async def _fail_locked(
        self,
        data: dict[str, Any],
        day: dict[str, Any],
        key: str,
        token: str,
        now: datetime,
        error: str,
        retry_minutes: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        generation = _normalize_generation(day.get("generation"))
        if generation.get("token") != token:
            return self._response(False, now, error="计划已在生成期间更新", conflict=True)
        attempts = int(generation.get("attempts", 0))
        generation.update(
            status="failed",
            error=str(error)[:240],
            next_retry_at=(now + timedelta(minutes=retry_minutes)).isoformat()
            if attempts < max_attempts else None,
            lease_until=None,
            token=None,
        )
        day["generation"] = generation
        day["updated_at"] = now.isoformat()
        data["revision"] += 1
        self._write_data(data)
        return self._response(False, now, error=generation["error"])

    def _clear_generation(self, day: dict[str, Any], *, ready: bool) -> None:
        generation = _normalize_generation(day.get("generation"))
        generation.update(
            status="ready" if ready else "idle",
            error="",
            next_retry_at=None,
            lease_until=None,
            token=None,
        )
        day["generation"] = generation

    async def replace_future(
        self,
        activities: Sequence[Mapping[str, Any]],
        *,
        expected_revision: int,
        now: datetime,
        session: Any = None,
        trusted: bool = False,
    ) -> dict[str, Any]:
        zone = self._zone()
        local_now = self._now(now, zone)
        if not self._enabled():
            return self._response(False, local_now, error="日常计划已禁用")
        if not self._authorized(session, trusted):
            return self._response(False, local_now, error="仅主人私聊可修改角色日常")
        if not isinstance(activities, Sequence) or isinstance(activities, (str, bytes)):
            return self._response(False, local_now, error="activities 必须是数组")
        date_text = local_now.date().isoformat()
        key = _day_key(date_text, zone.key)
        try:
            replacement = _normalize_activities(activities, date_text, zone.key)
            if len(replacement) > _MAX_ACTIVITIES:
                raise ValueError("一天最多安排 12 项活动")
            for activity in replacement:
                if _activity_start(activity, date_text, zone) <= local_now:
                    raise ValueError("只能提交尚未开始的未来活动")
        except (TypeError, ValueError) as exc:
            return self._response(False, local_now, error=str(exc))

        async with self._lock():
            data, error = self._read_data()
            if data is None:
                return self._response(False, local_now, error=error)
            if expected_revision != data["revision"]:
                return self._response(False, local_now, error="计划版本已变化，请刷新后重试", conflict=True)
            day = data["days"].get(key) or _new_day(date_text, zone.key)
            old = list(day.get("activities", []))
            protected = [
                activity for activity in old
                if activity.get("status") in {"done", "skipped"}
                or _activity_start(activity, date_text, zone) <= local_now
            ]
            combined = protected + replacement
            if len(combined) > _MAX_ACTIVITIES:
                return self._response(False, local_now, error="保留已开始活动后，一天最多安排 12 项")
            combined.sort(key=lambda item: item["start"])
            overlap = _validate_overlaps(combined)
            if overlap:
                return self._response(False, local_now, error=overlap)
            day["activities"] = combined
            self._clear_generation(day, ready=True)
            day["updated_at"] = local_now.isoformat()
            data["days"][key] = day
            data["revision"] += 1
            _prune_days(data["days"], keep_key=key)
            self._write_data(data)
            return self._response(True, local_now)

    async def record(
        self,
        activity_id: str,
        *,
        outcome: str = "",
        status: str = "done",
        expected_revision: int,
        now: datetime,
        session: Any = None,
        trusted: bool = False,
    ) -> dict[str, Any]:
        zone = self._zone()
        local_now = self._now(now, zone)
        if not self._enabled():
            return self._response(False, local_now, error="日常计划已禁用")
        if not self._authorized(session, trusted):
            return self._response(False, local_now, error="仅主人私聊可确认角色日常")
        if status not in {"done", "skipped"}:
            return self._response(False, local_now, error="status 只能是 done 或 skipped")
        if not isinstance(outcome, str) or len(outcome) > 160:
            return self._response(False, local_now, error="结果最多 160 字")
        key = _day_key(local_now.date().isoformat(), zone.key)

        async with self._lock():
            data, error = self._read_data()
            if data is None:
                return self._response(False, local_now, error=error)
            day = data["days"].get(key)
            if not isinstance(day, dict):
                return self._response(False, local_now, error="找不到这项日常")
            activity = next(
                (item for item in day.get("activities", []) if item.get("id") == activity_id),
                None,
            )
            if activity is None:
                return self._response(False, local_now, error="找不到这项日常")
            if activity.get("status") in {"done", "skipped"}:
                if activity.get("status") == status and activity.get("outcome", "") == outcome:
                    return self._response(True, local_now)
                return self._response(False, local_now, error="这项日常已经确认", conflict=True)
            if _activity_start(activity, key.split("|", 1)[0], zone) > local_now:
                return self._response(False, local_now, error="未来活动还不能确认")
            if expected_revision != data["revision"]:
                return self._response(False, local_now, error="计划版本已变化，请刷新后重试", conflict=True)
            activity["status"] = status
            activity["outcome"] = outcome
            self._clear_generation(day, ready=True)
            day["updated_at"] = local_now.isoformat()
            data["revision"] += 1
            self._write_data(data)
            return self._response(True, local_now)

    def _authorized(self, session: Any, trusted: bool) -> bool:
        if trusted:
            return True
        if session is None:
            return False
        is_private = getattr(session, "is_private", False)
        if callable(is_private):
            is_private = is_private()
        if not is_private:
            return False
        person_id_value = getattr(session, "person_id", "")
        person_id = person_id_value() if callable(person_id_value) else person_id_value
        person_id = str(person_id or "")
        peers = getattr(self.settings, "love_peers", ()) or ()
        if not person_id or person_id not in {str(peer) for peer in peers}:
            return False
        scope_settings = getattr(self.settings, "scope", None)
        mode = str(_get(scope_settings, "mode", "private") or "private")
        allowed, _reason = scope.allows(
            mode,
            is_private=True,
            person_id=person_id,
            love_peers=peers,
        )
        return bool(allowed)

    def context_line(self, *, now: datetime, budget: int = 110) -> str:
        try:
            limit = max(0, int(budget))
            if limit == 0:
                return ""
            snap = self.snapshot(now=now)
            if not snap["ok"] or not snap["enabled"] or not snap["activities"]:
                return ""
            bits = ["角色日常"]
            current = snap["current"]
            if current:
                bits.append(f"现在：{current['title']}")
            recent = snap["recent"]
            if recent:
                last = recent[0]
                detail = f"（{last['outcome']}）" if last.get("outcome") else ""
                bits.append(f"最近确认：{last['title']}{detail}")
            upcoming = snap["next"]
            if upcoming:
                bits.append(f"接下来：{upcoming['title']}")
            text = "；".join(bits)
            return text[:limit]
        except (TypeError, ValueError, OSError):
            return ""


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default) if value is not None else default


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"无效 JSON 常量 {value}")


def _day_key(date_text: str, timezone: str) -> str:
    return f"{date_text}|{timezone}"


def _new_day(date_text: str, timezone: str) -> dict[str, Any]:
    return {
        "date": date_text,
        "timezone": timezone,
        "activities": [],
        "generation": {
            "status": "idle",
            "error": "",
            "attempts": 0,
            "next_retry_at": None,
            "lease_until": None,
            "token": None,
        },
        "updated_at": None,
    }


def _normalize_generation(value: Any) -> dict[str, Any]:
    defaults = _new_day("2000-01-01", "UTC")["generation"]
    result = dict(defaults)
    if isinstance(value, Mapping):
        result.update(value)
    if result.get("status") not in {"idle", "generating", "ready", "failed", "disabled"}:
        result["status"] = "failed"
    try:
        result["attempts"] = max(0, int(result.get("attempts", 0)))
    except (TypeError, ValueError):
        result["attempts"] = 0
    result["error"] = str(result.get("error") or "")[:240]
    return result


def _generation_view(value: Any) -> dict[str, Any]:
    result = _normalize_generation(value)
    return {
        "status": result["status"],
        "error": result["error"],
        "attempts": result["attempts"],
        "next_retry_at": result.get("next_retry_at"),
        "max_attempts": 3,
    }


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _parse_time(value: Any, *, allow_end_24: bool = False) -> time:
    if not isinstance(value, str):
        raise ValueError("时间必须是 HH:MM 字符串")
    if allow_end_24 and value == "24:00":
        return time(23, 59, 59)
    if not _TIME_RE.fullmatch(value):
        raise ValueError(f"无效时间：{value!r}，必须使用 HH:MM")
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _stable_id(date_text: str, timezone: str, activity: Mapping[str, Any]) -> str:
    raw = "|".join(
        (date_text, timezone, activity["start"], activity["end"], activity["title"], activity["location"])
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _normalize_activities(
    activities: Sequence[Mapping[str, Any]], date_text: str, timezone: str
) -> list[dict[str, Any]]:
    if not 1 <= len(activities) <= _MAX_ACTIVITIES:
        if len(activities) == 0:
            return []
        raise ValueError("活动数量必须在 1 到 12 项之间")
    normalized: list[dict[str, Any]] = []
    for item in activities:
        if not isinstance(item, Mapping):
            raise ValueError("每项活动都必须是对象")
        start = item.get("start")
        end = item.get("end")
        start_time = _parse_time(start)
        end_time = _parse_time(end, allow_end_24=True)
        start_minutes = start_time.hour * 60 + start_time.minute
        end_minutes = 24 * 60 if end == "24:00" else end_time.hour * 60 + end_time.minute
        if end_minutes <= start_minutes:
            raise ValueError("活动结束时间必须晚于开始时间")
        title = item.get("title")
        location = item.get("location", "")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 60:
            raise ValueError("活动标题必须为 1 到 60 字")
        if not isinstance(location, str) or len(location.strip()) > 60:
            raise ValueError("活动地点最多 60 字")
        activity = {
            "start": start,
            "end": end,
            "title": title.strip(),
            "location": location.strip(),
            "status": "planned",
            "outcome": "",
        }
        activity["id"] = _stable_id(date_text, timezone, activity)
        normalized.append(activity)
    normalized.sort(key=lambda item: item["start"])
    overlap = _validate_overlaps(normalized)
    if overlap:
        raise ValueError(overlap)
    return normalized


def _parse_plan(response: Any, date_text: str, timezone: str) -> list[dict[str, Any]]:
    if not isinstance(response, str):
        raise ValueError("规划结果必须是 JSON 文本")
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"规划结果不是有效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("规划结果顶层必须是对象")
    if value.get("date") != date_text:
        raise ValueError("规划日期与当前日期不一致")
    if "timezone" in value and value.get("timezone") != timezone:
        raise ValueError("规划时区与当前时区不一致")
    activities = value.get("activities")
    if not isinstance(activities, list) or not 1 <= len(activities) <= _MAX_ACTIVITIES:
        raise ValueError("活动数量必须在 1 到 12 项之间")
    return _normalize_activities(activities, date_text, timezone)


def _validate_overlaps(activities: Sequence[Mapping[str, Any]]) -> str:
    previous_end = -1
    previous_title = ""
    for activity in sorted(activities, key=lambda item: item["start"]):
        start = _parse_time(activity.get("start"))
        end_value = activity.get("end")
        end = _parse_time(end_value, allow_end_24=True)
        start_min = start.hour * 60 + start.minute
        end_min = 24 * 60 if end_value == "24:00" else end.hour * 60 + end.minute
        if end_min <= start_min:
            return "活动结束时间必须晚于开始时间"
        if start_min < previous_end:
            return f"活动时间重叠：{previous_title}"
        previous_end = end_min
        previous_title = str(activity.get("title") or "活动")
    return ""


def _activity_start(activity: Mapping[str, Any], date_text: str, zone: ZoneInfo) -> datetime:
    parsed = _parse_time(activity["start"])
    return datetime.combine(date.fromisoformat(date_text), parsed, tzinfo=zone)


def _activity_end(activity: Mapping[str, Any], date_text: str, zone: ZoneInfo) -> datetime:
    if activity["end"] == "24:00":
        return datetime.combine(date.fromisoformat(date_text) + timedelta(days=1), time.min, tzinfo=zone)
    parsed = _parse_time(activity["end"])
    return datetime.combine(date.fromisoformat(date_text), parsed, tzinfo=zone)


def _public_activity(
    activity: Mapping[str, Any],
    date_text: str,
    now: datetime,
    *,
    include_date: bool = False,
) -> dict[str, Any]:
    zone = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else ZoneInfo(str(getattr(now.tzinfo, "key", "UTC")))
    start_at = _activity_start(activity, date_text, zone)
    end_at = _activity_end(activity, date_text, zone)
    stored_status = activity.get("status", "planned")
    if stored_status in {"done", "skipped"}:
        status = stored_status
    elif start_at <= now < end_at:
        status = "current"
    elif end_at <= now:
        status = "elapsed"
    else:
        status = "planned"
    result = {
        "id": str(activity["id"]),
        "start": str(activity["start"]),
        "end": str(activity["end"]),
        "title": str(activity["title"]),
        "location": str(activity.get("location", "")),
        "status": status,
        "outcome": str(activity.get("outcome", "")),
    }
    if include_date:
        result["date"] = date_text
    return result


def _validate_day_data(key: Any, day: Any) -> str:
    if not isinstance(key, str) or not isinstance(day, dict):
        return "日常计划 days 项格式无效"
    date_text = day.get("date")
    timezone = day.get("timezone")
    try:
        date.fromisoformat(date_text)
        ZoneInfo(timezone)
    except (TypeError, ValueError, KeyError):
        return f"日常计划日期或时区无效：{key}"
    if key != _day_key(date_text, timezone):
        return f"日常计划日期索引不匹配：{key}"
    activities = day.get("activities")
    if not isinstance(activities, list):
        return f"日常计划活动列表无效：{key}"
    if len(activities) > _MAX_ACTIVITIES:
        return f"日常计划活动数量过多：{key}"
    ids: set[str] = set()
    try:
        for activity in activities:
            if not isinstance(activity, dict):
                return f"日常计划活动格式无效：{key}"
            if not all(isinstance(activity.get(field), str) for field in ("id", "start", "end", "title", "location")):
                return f"日常计划活动字段无效：{key}"
            if activity["id"] in ids:
                return f"日常计划活动 id 重复：{key}"
            ids.add(activity["id"])
            _parse_time(activity["start"])
            _parse_time(activity["end"], allow_end_24=True)
            if not activity["title"] or len(activity["title"]) > 60 or len(activity["location"]) > 60:
                return f"日常计划活动文本长度无效：{key}"
            if activity.get("status") not in {"planned", "done", "skipped"}:
                return f"日常计划活动状态无效：{key}"
            if not isinstance(activity.get("outcome", ""), str) or len(activity.get("outcome", "")) > 160:
                return f"日常计划结果无效：{key}"
        overlap = _validate_overlaps(activities)
        if overlap:
            return f"日常计划活动冲突：{key}：{overlap}"
    except (TypeError, ValueError, KeyError):
        return f"日常计划活动时间无效：{key}"
    generation = day.get("generation")
    if not isinstance(generation, dict):
        return f"日常计划生成状态无效：{key}"
    if generation.get("status") not in {"idle", "generating", "ready", "failed", "disabled"}:
        return f"日常计划生成状态未知：{key}"
    attempts = generation.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        return f"日常计划尝试次数无效：{key}"
    return ""


def _prune_days(days: dict[str, Any], *, keep_key: str | None = None) -> None:
    if len(days) <= 7:
        return
    ordered = sorted(
        days,
        key=lambda key: (str(days[key].get("date", "")), str(days[key].get("updated_at", ""))),
    )
    keep = ordered[-7:]
    if keep_key in days and keep_key not in keep:
        keep = ordered[-6:] + [keep_key]
    keep_set = set(keep)
    for key in tuple(days):
        if key not in keep_set:
            del days[key]


__all__ = ["Life"]
