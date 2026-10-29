"""Public, read-only projection for the desktop pet state-sync contract."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import BindingMismatchError

_TTL_SECONDS = 60
_MAX_REFRESH_SECONDS = 15
_MOOD_STATUSES = {"fresh", "stale", "empty", "disabled", "unavailable"}


def build_snapshot(
    life: Any,
    state: Any,
    settings: Any,
    binding: Any,
    now: datetime,
) -> dict[str, Any]:
    """Project only life current/next and the state's public mood into schema v1."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    configured_agent = _pet_bridge_agent(settings)
    device_id = _binding_text(binding, "device_id")
    principal_id = _binding_text(binding, "principal_id")
    bound_agent = _binding_text(binding, "agent_id")
    owners = _get(settings, "love_peers", None)
    principal_is_allowed = owners is None or (
        isinstance(owners, (list, tuple, set)) and principal_id in owners
    )
    if (
        not device_id
        or not principal_id
        or not bound_agent
        or bound_agent != configured_agent
        or not principal_is_allowed
    ):
        raise BindingMismatchError("device binding does not match the configured agent")

    zone, zone_name = _server_zone(settings)
    local_now = now.astimezone(zone)
    server_time = local_now.isoformat()
    valid_until = _add_seconds(local_now, _TTL_SECONDS).isoformat()

    life_status = "unavailable"
    life_enabled = False
    life_date = local_now.date().isoformat()
    revision: int | None = None
    current: dict[str, Any] | None = None
    next_activity: dict[str, Any] | None = None
    activity_boundaries: list[datetime] = []
    try:
        life_data = life.snapshot(now=now)
        if not isinstance(life_data, Mapping):
            raise ValueError("invalid life snapshot")
        candidate_date = life_data.get("date")
        if not isinstance(candidate_date, str):
            raise ValueError("invalid life date")
        life_date = date.fromisoformat(candidate_date).isoformat()
        if life_date != local_now.date().isoformat():
            raise ValueError("life date does not match server time")
        if life_data.get("timezone") != zone_name:
            raise ValueError("life timezone does not match server timezone")
        raw_enabled = life_data.get("enabled")
        if not isinstance(raw_enabled, bool):
            raise ValueError("invalid life enabled flag")
        life_enabled = bool(raw_enabled) and _settings_enabled(settings)
        raw_revision = life_data.get("revision")
        if isinstance(raw_revision, int) and not isinstance(raw_revision, bool) and raw_revision >= 0:
            revision = raw_revision

        if not life_enabled:
            life_status = "disabled"
        elif life_data.get("ok") is not True:
            life_status = "unavailable"
            revision = None
        else:
            if revision is None:
                raise ValueError("invalid life revision")
            raw_current = life_data.get("current")
            raw_next = life_data.get("next")
            if raw_current is not None and not isinstance(raw_current, Mapping):
                raise ValueError("invalid current activity")
            if raw_next is not None and not isinstance(raw_next, Mapping):
                raise ValueError("invalid next activity")
            current = _project_activity(raw_current, life_date, zone, "current") if raw_current is not None else None
            next_activity = _project_activity(raw_next, life_date, zone, "planned") if raw_next is not None else None
            if isinstance(raw_current, Mapping):
                activity_boundaries.extend(_activity_boundaries(raw_current, life_date, zone, local_now))
            if isinstance(raw_next, Mapping):
                activity_boundaries.extend(_activity_boundaries(raw_next, life_date, zone, local_now))

            activities = life_data.get("activities")
            if not isinstance(activities, list):
                raise ValueError("invalid life activity list")
            has_plan = bool(activities)
            generation = life_data.get("generation")
            is_planning = isinstance(generation, Mapping) and generation.get("status") == "generating"
            life_status = "ready" if has_plan else ("planning" if is_planning else "empty")
    except Exception:
        life_status = "unavailable"
        life_enabled = _settings_enabled(settings)
        revision = None
        current = None
        next_activity = None
        activity_boundaries = []

    if life_status in {"disabled", "unavailable"}:
        current = None
        next_activity = None

    mood = _public_mood(state, now)
    refresh_after = _refresh_seconds(now, activity_boundaries, mood, zone)
    return {
        "schema_version": 1,
        "agent_id": bound_agent,
        "device_id": device_id,
        "server_time": server_time,
        "timezone": zone_name,
        "ttl_seconds": _TTL_SECONDS,
        "valid_until": valid_until,
        "refresh_after_seconds": refresh_after,
        "life": {
            "enabled": life_enabled,
            "status": life_status,
            "date": life_date,
            "revision": revision,
            "current": current,
            "next": next_activity,
        },
        "mood": mood,
        "presentation": {"expression": "neutral"},
    }


def _public_mood(state: Any, now: datetime) -> dict[str, Any]:
    empty = {"status": "unavailable", "word": None, "observed_at": None, "valid_until": None}
    method = getattr(state, "public_mood", None)
    if not callable(method):
        return empty
    try:
        raw = method(now=now)
    except Exception:
        return empty
    if not isinstance(raw, Mapping):
        return empty
    status = raw.get("status")
    if status not in _MOOD_STATUSES:
        return empty
    word = raw.get("word")
    observed = raw.get("observed_at")
    expires = raw.get("valid_until")
    if word is not None and not isinstance(word, str):
        return empty
    if observed is not None and not isinstance(observed, str):
        return empty
    if expires is not None and not isinstance(expires, str):
        return empty
    observed_at = _parse_aware_datetime(observed) if observed is not None else None
    valid_until = _parse_aware_datetime(expires) if expires is not None else None
    if (observed is not None and observed_at is None) or (expires is not None and valid_until is None):
        return empty
    if status in {"empty", "disabled", "unavailable"}:
        return {"status": status, "word": None, "observed_at": None, "valid_until": None}
    if status == "stale":
        return {
            "status": "stale",
            "word": None,
            "observed_at": observed,
            "valid_until": expires,
        }
    if not word:
        return {"status": "empty", "word": None, "observed_at": None, "valid_until": None}
    elif observed_at is None or valid_until is None or observed_at > now:
        return empty
    elif valid_until <= observed_at:
        return empty
    elif valid_until <= now:
        return {
            "status": "stale",
            "word": None,
            "observed_at": observed,
            "valid_until": expires,
        }
    return {
        "status": status,
        "word": word[:60] if isinstance(word, str) else None,
        "observed_at": observed,
        "valid_until": expires,
    }


def _project_activity(raw: Any, date_text: str, zone: ZoneInfo, status: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("invalid activity")
    if raw.get("status") != status:
        raise ValueError("activity status does not match its snapshot slot")
    identifier = raw.get("id")
    title = raw.get("title")
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128:
        raise ValueError("invalid activity id")
    if not isinstance(title, str) or not title.strip() or len(title) > 60:
        raise ValueError("invalid activity title")
    location = raw.get("location")
    if not isinstance(location, str):
        location = ""
    if len(location) > 60:
        raise ValueError("invalid activity location")
    start_at = _activity_datetime(date_text, raw.get("start"), zone, allow_24=False)
    end_at = _activity_datetime(date_text, raw.get("end"), zone, allow_24=True)
    if end_at <= start_at:
        raise ValueError("invalid activity range")
    return {
        "id": identifier,
        "title": title,
        "location": location or None,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "status": status,
    }


def _activity_datetime(
    date_text: str, value: Any, zone: ZoneInfo, *, allow_24: bool
) -> datetime:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("invalid activity time")
    if value == "24:00" and allow_24:
        day = date.fromisoformat(date_text) + timedelta(days=1)
        return datetime.combine(day, time.min, tzinfo=zone)
    hour_text, minute_text = value.split(":", 1)
    if len(hour_text) != 2 or len(minute_text) != 2 or not hour_text.isdigit() or not minute_text.isdigit():
        raise ValueError("invalid activity time")
    hour, minute = int(hour_text), int(minute_text)
    if hour > 23 or minute > 59:
        raise ValueError("invalid activity time")
    return datetime.combine(date.fromisoformat(date_text), time(hour, minute), tzinfo=zone)


def _activity_boundaries(
    raw: Mapping[str, Any], date_text: str, zone: ZoneInfo, now: datetime
) -> list[datetime]:
    starts = _activity_datetime(date_text, raw.get("start"), zone, allow_24=False)
    ends = _activity_datetime(date_text, raw.get("end"), zone, allow_24=True)
    utc_now = now.astimezone(timezone.utc)
    return [item for item in (starts, ends) if item.astimezone(timezone.utc) > utc_now]


def _refresh_seconds(
    now: datetime,
    boundaries: list[datetime],
    mood: Mapping[str, Any],
    zone: ZoneInfo,
) -> int:
    candidates = list(boundaries)
    if mood.get("status") == "fresh" and isinstance(mood.get("valid_until"), str):
        try:
            expiry = datetime.fromisoformat(mood["valid_until"])
        except ValueError:
            expiry = None
        if expiry is not None and expiry.tzinfo is not None and expiry.utcoffset() is not None:
            candidates.append(expiry)

    interval = _MAX_REFRESH_SECONDS
    utc_now = now.astimezone(timezone.utc)
    for boundary in candidates:
        delta = (boundary.astimezone(timezone.utc) - utc_now).total_seconds()
        if delta > 0:
            interval = min(interval, max(1, math.ceil(delta)))

    local_now = now.astimezone(zone)
    midnight = datetime.combine(
        local_now.date() + timedelta(days=1),
        time.min,
        tzinfo=zone,
    )
    delta = (midnight.astimezone(timezone.utc) - utc_now).total_seconds()
    if delta > 0:
        interval = min(interval, max(1, math.ceil(delta)))
    return min(_MAX_REFRESH_SECONDS, max(1, interval))


def _add_seconds(value: datetime, seconds: int) -> datetime:
    return (value.astimezone(timezone.utc) + timedelta(seconds=seconds)).astimezone(value.tzinfo)


def _parse_aware_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _settings_enabled(settings: Any) -> bool:
    return bool(_get(settings, "enabled", True))


def _pet_bridge_agent(settings: Any) -> str:
    config = _get(settings, "pet_bridge", None)
    value = _get(config, "agent_id", None)
    return value.strip() if isinstance(value, str) else ""


def _binding_text(binding: Any, key: str) -> str:
    value = _get(binding, key, None)
    return value.strip() if isinstance(value, str) else ""


def _get(value: Any, key: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _server_zone(settings: Any) -> tuple[ZoneInfo, str]:
    zone_method = getattr(settings, "zone", None)
    try:
        zone = zone_method() if callable(zone_method) else None
    except Exception:
        zone = None
    if isinstance(zone, ZoneInfo):
        return zone, zone.key
    key = getattr(zone, "key", None) or _get(settings, "timezone", None)
    if not isinstance(key, str) or not key:
        key = "UTC"
    try:
        zone = ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        zone = ZoneInfo("UTC")
        key = "UTC"
    return zone, key


__all__ = ["build_snapshot"]
