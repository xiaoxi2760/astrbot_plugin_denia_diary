"""Diary v1 facade. Event ownership is verified before any state is accessed.

This deduplicates hooks within one AstrBot event, including direct-call fallback.
It is deliberately not the persistent request ledger required for device chat.
"""
from __future__ import annotations

import asyncio


class EmptyDiary:
    def prompt_parts(self, *args, **kwargs):
        return "", ""

    def event_hint(self, *args, **kwargs):
        return ""


class NotebookWithoutFacts:
    def __init__(self, notebook):
        self.notebook = notebook

    def prompt_lines(self, *args, **kwargs):
        promise, _ = self.notebook.prompt_lines(*args, **kwargs)
        return promise, ""


class DiaryServiceV1:
    protocol_version = 1
    capabilities = ("turn.prepare", "prompt.claim", "context.read")
    _key = "_denia_diary_turn_v1"

    def __init__(self, plugin):
        self._plugin = plugin
        self._generation = object()
        self.closed = False
        self._pending = set()

    async def close(self):
        self.closed = True
        if self._pending:
            done, pending = await asyncio.wait(tuple(self._pending), timeout=30)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def isolation_enabled(self):
        return self._plugin._manager.isolation_on

    def _record(self, event):
        if self.closed:
            return None, None
        plugin = self._plugin
        runtime = plugin._manager.resolve_event(event)
        if runtime is None:
            return None, None
        session = plugin._session(event)
        identity = (runtime.scope, session.umo, session.person_id())
        getter = getattr(event, "get_extra", None)
        value = getter(self._key) if callable(getter) else getattr(event, self._key, None)
        if not isinstance(value, dict) or value.get("protocol_version") != 1:
            value = {"protocol_version": 1, "generation": self._generation, "identity": identity,
                     "runtime": runtime, "task": None, "result": None, "requests": []}
            setter = getattr(event, "set_extra", None)
            if callable(setter):
                setter(self._key, value)
            else:
                setattr(event, self._key, value)
        if value["identity"] != identity:
            return None, None
        return runtime, value

    async def prepare_turn(self, event, *, query_text=None, request=None, features=None):
        runtime, record = self._record(event)
        if record is None:
            return {"ok": False, "protocol_version": 1, "text": "", "reason": "instance_unavailable"}
        if record["task"] is None and record["result"] is None:
            task = asyncio.create_task(self._prepare_record(record, runtime, event, query_text, request))
            record["task"] = task
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        if record["task"] is not None:
            # The one owned preparation survives cancellation of an individual waiter.
            task = record["task"]
            if not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
        result = dict(record["result"])
        result.setdefault("text", "")
        if result.get("ok"):
            with self._plugin._bind_runtime(runtime):
                result["text"] = self._plugin._render_diary_text(record["base"], features=features)
        return result

    async def _prepare_record(self, record, runtime, event, query_text, request):
        record["result"] = {"ok": False, "protocol_version": 1, "text": "", "reason": "prepare_incomplete"}
        try:
            with self._plugin._bind_runtime(runtime):
                record["base"] = await self._plugin._prepare_diary_turn(event, query_text=query_text, req=request)
            record["result"] = {"ok": True, "protocol_version": 1,
                "scope_id": runtime.scope, "subject_id": record["identity"][2]}
        except asyncio.CancelledError:
            record["result"]["reason"] = "prepare_cancelled"
            raise
        except Exception:
            record["result"]["reason"] = "prepare_failed"

    def _request_entry(self, record, request):
        for entry in record["requests"]:
            if entry["request"] is request:
                return entry
        entry = {"request": request, "owner": "", "parts": [], "suffix": ""}
        record["requests"].append(entry)
        return entry

    async def inject_request(self, event, request):
        result = await self.prepare_turn(event, request=request)
        if not result.get("ok") or not result.get("text"):
            return result
        runtime, record = self._record(event)
        if record is None:
            return {"ok": False, "text": "", "reason": "instance_changed"}
        entry = self._request_entry(record, request)
        if entry["owner"]:
            return result
        entry["owner"] = "diary"
        previous = list(getattr(request, "extra_user_content_parts", None) or [])
        previous_system = str(getattr(request, "system_prompt", "") or "")
        with self._plugin._bind_runtime(runtime):
            self._plugin._attach_hint(request, result["text"])
        entry["parts"] = [part for part in getattr(request, "extra_user_content_parts", None) or []
                          if all(part is not old for old in previous)]
        current = str(getattr(request, "system_prompt", "") or "")
        if current == previous_system + result["text"]:
            entry["suffix"] = result["text"]
        return result

    def claim_prompt(self, event, request):
        """Core will insert the returned text once; remove only our own prior part."""
        _, record = self._record(event)
        if record is None:
            return False
        entry = self._request_entry(record, request)
        owned = entry["parts"]
        parts = getattr(request, "extra_user_content_parts", None)
        if isinstance(parts, list) and owned:
            request.extra_user_content_parts = [part for part in parts if all(part is not item for item in owned)]
        suffix = entry["suffix"]
        system = str(getattr(request, "system_prompt", "") or "")
        if suffix and system.endswith(suffix):
            request.system_prompt = system[:-len(suffix)]
        entry.update(owner="core", parts=[], suffix="")
        return True

    def read_context(self, event):
        if self.closed:
            return {"ok": False, "protocol_version": 1}
        runtime = self._plugin._manager.resolve_event(event)
        if runtime is None:
            return {"ok": False, "protocol_version": 1}
        with self._plugin._bind_runtime(runtime):
            session = self._plugin._session(event)
            if self._plugin._scope_denied_reason(session):
                return {"ok": False, "protocol_version": 1}
            return {"ok": True, "protocol_version": 1, "scope_id": runtime.scope,
                    "subject_id": session.person_id(),
                    "mood": dict(runtime.state.public_mood() or {}),
                    "affinity_band": str(runtime.affinity.band(session.person_id()) or "")}
