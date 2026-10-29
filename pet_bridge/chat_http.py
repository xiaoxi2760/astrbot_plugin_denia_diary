"""Bound v2 text chat routes on the existing opt-in companion listener."""
from __future__ import annotations

import json
import re
from collections import OrderedDict, deque
from time import monotonic

from .server import _BEARER_RE, _call

_PREFIX = "/api/pet/v2"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class PetChatHTTP:
    def __init__(self, parent, callbacks):
        self.parent = parent
        self.callbacks = dict(callbacks)
        self.devices = OrderedDict()

    def register(self, app):
        app.router.add_route("*", _PREFIX + "/capabilities", self.capabilities)
        app.router.add_route("*", _PREFIX + "/chat", self.chat)
        app.router.add_route("*", _PREFIX + "/turns/{request_id}", self.query)

    def _json(self, status, data):
        from aiohttp import web
        try:
            body = json.dumps({"protocol_version": 2, **data}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > 65536:
                raise ValueError("Oversized response")
        except (ValueError, TypeError, UnicodeError):
            status, body = 503, b'{"protocol_version":2,"ok":false,"error_code":"unavailable"}'
        return web.Response(status=status, body=body, content_type="application/json", charset="utf-8", headers={"Cache-Control": "no-store"})

    def _error(self, status, code):
        return self._json(status, {"ok": False, "error_code": code})

    async def _authorize(self, request, method, permission=None):
        if self.parent._check_request_rate(request.remote) is not None:
            return None, self._error(429, "rate_limited")
        if request.method != method:
            return None, self._error(405, "method_not_allowed")
        match = _BEARER_RE.fullmatch(request.headers.get("Authorization", ""))
        if match is None or request.query_string:
            return None, self._error(401, "unauthorized")
        if request.headers.get("Origin") is not None:
            return None, self._error(403, "forbidden")
        try:
            binding = await _call(self.callbacks["authenticate"], match.group(1))
        except PermissionError:
            return None, self._error(403, "forbidden")
        except Exception:
            return None, self._error(503, "unavailable")
        if not isinstance(binding, dict):
            return None, self._error(401, "unauthorized")
        if any(not isinstance(binding.get(key), str) or not binding[key] or len(binding[key]) > 256 for key in ("device_id", "agent_id", "scope_id", "platform_subject_id", "preset_id", "namespace")):
            return None, self._error(403, "binding_mismatch")
        device = binding.get("device_id")
        if not isinstance(device, str) or not device.startswith("dv2_") or not isinstance(binding.get("permissions"), (list, tuple)):
            return None, self._error(403, "forbidden")
        if permission and permission not in binding["permissions"]:
            return None, self._error(403, "forbidden")
        now = monotonic()
        events = self.devices.pop(device, deque())
        while events and events[0] <= now - 1:
            events.popleft()
        if len(events) >= 8:
            self.devices[device] = events
            return None, self._error(429, "rate_limited")
        events.append(now)
        self.devices[device] = events
        while len(self.devices) > 32:
            self.devices.popitem(last=False)
        return binding, None

    async def _active(self, binding):
        return await _call(self.callbacks["active"], binding) is True

    async def capabilities(self, request):
        binding, error = await self._authorize(request, "GET")
        if error is not None:
            return error
        try:
            data = await _call(self.callbacks["capabilities"], binding)
            if not await self._active(binding):
                return self._error(403, "forbidden")
            if not isinstance(data, dict) or data.get("agent_id") != binding.get("agent_id"):
                return self._error(403, "binding_mismatch")
            return self._json(200, {**data, "ok": True, "device_id": binding["device_id"]})
        except PermissionError:
            return self._error(403, "forbidden")
        except Exception:
            return self._error(503, "unavailable")

    async def chat(self, request):
        binding, error = await self._authorize(request, "POST", "chat:write")
        if error is not None:
            return error
        try:
            if request.content_length is not None and request.content_length > 16384:
                return self._error(413, "request_too_large")
            raw = await request.read()
            if len(raw) > 16384:
                return self._error(413, "request_too_large")
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
            if not isinstance(body, dict) or set(body) != {"request_id", "text"}:
                return self._error(400, "invalid_request")
            request_id, text = body["request_id"], body["text"]
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id) or not isinstance(text, str) or not text.strip() or len(text) > 4000:
                return self._error(400, "invalid_request")
            if len(text.encode("utf-8")) > 16000 or any(ord(char) < 32 and char not in "\n\t" for char in text):
                return self._error(400, "invalid_request")
        except Exception:
            return self._error(400, "invalid_request")
        return await self._execute(binding, request_id, self.callbacks["chat"], text, request_id)

    async def query(self, request):
        binding, error = await self._authorize(request, "GET", "turn:read")
        if error is not None:
            return error
        request_id = request.match_info.get("request_id", "")
        if not _REQUEST_ID.fullmatch(request_id):
            return self._error(400, "invalid_request")
        return await self._execute(binding, request_id, self.callbacks["query"], request_id)

    async def _execute(self, binding, request_id, callback, *args):
        try:
            if not await self._active(binding):
                return self._error(403, "forbidden")
            record = await _call(callback, binding, *args)
            if not await self._active(binding):
                return self._error(403, "forbidden")
            if record is None:
                return self._error(404, "turn_not_found")
            if not isinstance(record, dict) or record.get("request_id") != request_id:
                return self._error(503, "unavailable")
            status = record.get("status")
            if status == "completed":
                result = record.get("result")
                if not isinstance(result, dict) or result.get("agent_id") != binding["agent_id"] or result.get("request_id") != request_id or result.get("turn_id") != record.get("turn_id"):
                    return self._error(403, "binding_mismatch")
                return self._json(200, {**result, "ok": True, "status": status, "device_id": binding["device_id"]})
            if status not in {"accepted", "generating", "settling", "failed", "uncertain"}:
                return self._error(503, "unavailable")
            pending = status in {"accepted", "generating", "settling"}
            return self._json(202 if pending else 409, {"ok": pending, "request_id": request_id,
                "turn_id": record.get("turn_id"), "status": status, "error_code": record.get("error_code", ""), "device_id": binding["device_id"]})
        except PermissionError:
            return self._error(403, "forbidden")
        except ValueError:
            return self._error(409, "request_conflict")
        except Exception:
            return self._error(503, "unavailable")
