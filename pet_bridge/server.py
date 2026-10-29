"""Isolated HTTP listener for the authenticated, read-only pet snapshot."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections import OrderedDict, deque
from collections.abc import Mapping
from time import monotonic
from typing import Any, Callable

try:
    from ..core.pet_state.errors import BindingMismatchError
except ImportError:  # Support the repository's top-level `pet_bridge` test import.
    from core.pet_state.errors import BindingMismatchError

_PATH = "/api/pet/v1/snapshot"
_MAX_RESPONSE_BYTES = 32768
_SOURCE_REQUESTS_PER_SECOND = 32
_GLOBAL_REQUESTS_PER_SECOND = 64
_MAX_SOURCE_BUCKETS = 128
_BEARER_RE = re.compile(r"^Bearer ([A-Za-z0-9._~-]{1,512})$", re.IGNORECASE)


class PetStateServer:
    """Manage a dedicated aiohttp listener without importing aiohttp until start()."""

    def __init__(
        self,
        snapshot_callback: Callable[..., Any],
        authenticate_callback: Callable[[str], Any],
        *, v2_callbacks: dict | None = None,
    ) -> None:
        self._snapshot_callback = snapshot_callback
        self._authenticate_callback = authenticate_callback
        self._v2_callbacks = dict(v2_callbacks) if v2_callbacks else None
        self._runner: Any = None
        self._site: Any = None
        self._host: str | None = None
        self._port: int | None = None
        self._error: str | None = None
        self._source_requests: OrderedDict[str, deque[float]] = OrderedDict()
        self._global_requests: deque[float] = deque()
        self._device_requests: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def start(self, host: str, port: int) -> dict[str, Any]:
        async with self._lock:
            if self._runner is not None:
                return self.status()
            self._error = None
            runner = None
            try:
                from aiohttp import web

                app = web.Application(client_max_size=32768)
                app.router.add_route("*", _PATH, self._handle)
                if self._v2_callbacks:
                    from .chat_http import PetChatHTTP
                    PetChatHTTP(self, self._v2_callbacks).register(app)
                app.router.add_route("*", "/{tail:.*}", self._not_found)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                site = web.TCPSite(runner, host=host, port=port)
                await site.start()
                sockets = getattr(getattr(site, "_server", None), "sockets", None) or []
                actual_host = host
                actual_port = port
                if sockets:
                    address = sockets[0].getsockname()
                    actual_host, actual_port = str(address[0]), int(address[1])
                self._runner = runner
                self._site = site
                self._host = actual_host
                self._port = actual_port
                self._clear_rate_state()
                self._device_requests.clear()
                return self.status()
            except Exception:
                if runner is not None:
                    try:
                        await runner.cleanup()
                    except Exception:
                        pass
                self._runner = None
                self._site = None
                self._host = None
                self._port = None
                self._error = "listener unavailable"
                return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            runner = self._runner
            self._runner = None
            self._site = None
            self._host = None
            self._port = None
            self._clear_rate_state()
            self._device_requests.clear()
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    self._error = "listener cleanup failed"
                else:
                    self._error = None
            self._clear_rate_state()
            self._device_requests.clear()
            return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "running": self._runner is not None,
            "host": self._host,
            "port": self._port,
            "error": self._error,
        }

    async def _handle(self, request: Any) -> Any:
        from aiohttp import web

        # This gate uses the peer address reported by aiohttp itself. Forwarded
        # headers are deliberately ignored because the listener has no trusted proxy.
        if self._check_request_rate(request.remote) is not None:
            response = _response(web, 429, "rate_limited", "无法读取角色状态")
            response.headers["Retry-After"] = "1"
            return response

        if request.method != "GET":
            return _response(web, 405, "method_not_allowed", "无法读取角色状态")

        match = _BEARER_RE.fullmatch(request.headers.get("Authorization", ""))
        if match is None:
            return _response(web, 401, "unauthorized", "无法读取角色状态")
        if request.query_string:
            return _response(web, 401, "unauthorized", "无法读取角色状态")
        credential = match.group(1)
        try:
            binding = await _call(self._authenticate_callback, credential)
        except (BindingMismatchError, PermissionError):
            return _response(web, 403, "forbidden", "无法读取角色状态")
        except Exception:
            return _response(web, 503, "unavailable", "状态同步暂不可用")
        if binding is None:
            return _response(web, 401, "unauthorized", "无法读取角色状态")
        if request.headers.get("Origin") is not None:
            return _response(web, 403, "forbidden", "无法读取角色状态")
        if not isinstance(binding, Mapping):
            return _response(web, 403, "forbidden", "无法读取角色状态")
        device_id = binding.get("device_id")
        agent_id = binding.get("agent_id")
        principal_id = binding.get("principal_id")
        if not all(isinstance(value, str) and value for value in (device_id, agent_id, principal_id)):
            return _response(web, 403, "forbidden", "无法读取角色状态")

        retry_after = self._check_device_rate(device_id)
        if retry_after is not None:
            response = _response(web, 429, "rate_limited", "无法读取角色状态")
            response.headers["Retry-After"] = str(retry_after)
            return response
        try:
            snapshot = await _call(self._snapshot_callback, dict(binding))
        except (BindingMismatchError, PermissionError):
            return _response(web, 403, "forbidden", "无法读取角色状态")
        except Exception:
            return _response(web, 503, "unavailable", "状态同步暂不可用")
        if not isinstance(snapshot, Mapping):
            return _response(web, 503, "unavailable", "状态同步暂不可用")
        if snapshot.get("agent_id") != agent_id or snapshot.get("device_id") != device_id:
            return _response(web, 403, "forbidden", "无法读取角色状态")
        try:
            body = json.dumps(
                snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            return _response(web, 503, "unavailable", "状态同步暂不可用")
        if len(body) > _MAX_RESPONSE_BYTES:
            return _response(web, 503, "unavailable", "状态同步暂不可用")
        return web.Response(
            status=200,
            body=body,
            content_type="application/json",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def _not_found(self, request: Any) -> Any:
        from aiohttp import web

        return _response(web, 404, "not_found", "无法读取角色状态")

    def _check_request_rate(self, remote: Any) -> int | None:
        """Limit pre-auth work with bounded per-peer and global rolling windows."""
        now = monotonic()
        cutoff = now - 1.0
        while self._global_requests and self._global_requests[0] <= cutoff:
            self._global_requests.popleft()

        # request.remote is supplied by the transport. Never key this by
        # X-Forwarded-For or another client-controlled header.
        source = remote if isinstance(remote, str) and remote else "<unknown>"
        source = source[:128]

        for key, events in tuple(self._source_requests.items()):
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                del self._source_requests[key]

        events = self._source_requests.get(source)
        if events is None:
            if len(self._source_requests) >= _MAX_SOURCE_BUCKETS:
                self._source_requests.popitem(last=False)
            events = deque()
            self._source_requests[source] = events
        else:
            self._source_requests.move_to_end(source)

        source_limited = len(events) >= _SOURCE_REQUESTS_PER_SECOND
        global_limited = len(self._global_requests) >= _GLOBAL_REQUESTS_PER_SECOND
        if source_limited or global_limited:
            # Count the attempt in the bucket that still has room. This prevents
            # a global burst from being followed by an immediate per-peer burst.
            if not source_limited:
                events.append(now)
            if not global_limited:
                self._global_requests.append(now)
            return 1

        events.append(now)
        self._global_requests.append(now)
        return None

    def _clear_rate_state(self) -> None:
        self._source_requests.clear()
        self._global_requests.clear()

    def _check_device_rate(self, device_id: str) -> int | None:
        now = monotonic()
        cutoff = now - 1.0
        events = self._device_requests.get(device_id)
        if events is None:
            if len(self._device_requests) >= 32:
                oldest_key = min(
                    self._device_requests,
                    key=lambda key: self._device_requests[key][-1] if self._device_requests[key] else 0,
                )
                self._device_requests.pop(oldest_key, None)
            events = deque(maxlen=1)
            self._device_requests[device_id] = events
        while events and events[0] <= cutoff:
            events.popleft()
        if events:
            return 1
        events.append(now)
        return None


async def _call(callback: Callable[..., Any], *args: Any) -> Any:
    result = callback(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _response(web: Any, status: int, code: str, message: str) -> Any:
    body = json.dumps(
        {"schema_version": 1, "error": {"code": code, "message": message}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return web.Response(
        status=status,
        body=body,
        content_type="application/json",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["PetStateServer"]
