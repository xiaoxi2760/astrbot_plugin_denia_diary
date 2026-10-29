"""Separate v2 device grants. Legacy read-only credentials are never upgraded."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime

from .devices import DeviceStore, _IssueResult, _OneTimeCredential, _aware_iso, _required_text, _reject_json_constant
from .errors import DeviceStoreError

PERMISSIONS = frozenset({"state:read", "chat:write", "turn:read", "diary:read", "diary:write", "life:read", "life:write"})
_BINDING = ("scope_id", "agent_id", "platform_subject_id", "preset_id", "namespace")
_FIELDS = {"device_id", "name", "token_hash", "created_at", "permissions", *_BINDING}
_MAX_BYTES = 128 * 1024


class ChatDeviceStore(DeviceStore):
    def issue(self, name, binding, permissions, now):
        label = _required_text(name, "name", 64)
        resolved = {key: _required_text(binding.get(key), key, 256 if key != "agent_id" else 64) for key in _BINDING}
        if any(resolved[key] != binding[key] for key in _BINDING):
            raise ValueError("Binding identifiers must preserve their exact storage keys")
        grants = _permissions(permissions)
        if "chat:write" in grants and "turn:read" not in grants:
            raise ValueError("Chat requires permission to query the original turn")
        credential = "denia2." + secrets.token_urlsafe(32)
        record = {**resolved, "device_id": "dv2_" + uuid.uuid4().hex, "name": label,
                  "permissions": grants, "created_at": _aware_iso(now),
                  "token_hash": hashlib.sha256(credential.encode("ascii")).hexdigest()}
        with self._lock:
            data = self._read()
            if len(data["devices"]) >= 16:
                raise DeviceStoreError("Active v2 device limit reached")
            data["devices"].append(record)
            self._write(data)
        return _IssueResult(ok=True, **_public(record), credential=_OneTimeCredential(credential))

    def authenticate(self, credential):
        if not isinstance(credential, str) or not re.fullmatch(r"denia2\.[A-Za-z0-9_-]{43}", credential):
            return None
        digest = hashlib.sha256(credential.encode("ascii")).hexdigest()
        with self._lock:
            try:
                data = self._read()
            except DeviceStoreError:
                return None
            found = None
            for record in data["devices"]:
                if hmac.compare_digest(record["token_hash"], digest):
                    found = _public(record)
            return found

    def is_active(self, binding):
        with self._lock:
            try:
                data = self._read()
            except DeviceStoreError:
                return False
            return any(_public(record) == binding for record in data["devices"])

    def list_devices(self):
        with self._lock:
            return [_public(record) for record in self._read()["devices"]]

    def _read(self):
        try:
            if self.path.is_symlink() or self.path.is_junction():
                raise DeviceStoreError("V2 device ledger path is unavailable")
            with self.path.open("rb") as source:
                raw = source.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise DeviceStoreError("V2 device ledger is too large")
            data = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except FileNotFoundError:
            return {"schema": 2, "devices": []}
        except DeviceStoreError:
            raise
        except (OSError, UnicodeError, ValueError) as error:
            raise DeviceStoreError("V2 device ledger is unavailable") from error
        _validate(data)
        return data

    def _write(self, data):
        _validate(data)
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > _MAX_BYTES:
            raise DeviceStoreError("V2 device ledger is too large")
        super()._write(data)


def _permissions(value):
    if not isinstance(value, (list, tuple)) or not value or any(not isinstance(item, str) or item not in PERMISSIONS for item in value) or len(set(value)) != len(value):
        raise ValueError("Invalid v2 permissions")
    return sorted(value)


def _public(record):
    return {key: copy.deepcopy(value) for key, value in record.items() if key != "token_hash"}


def _validate(data):
    if not isinstance(data, dict) or set(data) != {"schema", "devices"} or type(data["schema"]) is not int or data["schema"] != 2 or not isinstance(data["devices"], list) or len(data["devices"]) > 16:
        raise DeviceStoreError("Invalid v2 device ledger")
    ids, hashes = set(), set()
    try:
        for record in data["devices"]:
            if not isinstance(record, dict) or set(record) != _FIELDS:
                raise ValueError("Invalid record fields")
            for key in (*_BINDING, "device_id", "name"):
                maximum = 256 if key in _BINDING and key != "agent_id" else 64
                value = _required_text(record[key], key, maximum)
                if value != record[key] or any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise ValueError("Invalid identifier")
            if not re.fullmatch(r"dv2_[0-9a-f]{32}", record["device_id"]) or not isinstance(record["token_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["token_hash"]):
                raise ValueError("Invalid device identity")
            if record["device_id"] in ids or record["token_hash"] in hashes:
                raise ValueError("Duplicate device identity")
            ids.add(record["device_id"])
            hashes.add(record["token_hash"])
            if _permissions(record["permissions"]) != record["permissions"]:
                raise ValueError("Unnormalized permissions")
            if "chat:write" in record["permissions"] and "turn:read" not in record["permissions"]:
                raise ValueError("Chat needs turn queries")
            created = datetime.fromisoformat(record["created_at"])
            if created.tzinfo is None or created.utcoffset() is None:
                raise ValueError("Invalid timestamp")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise DeviceStoreError("Invalid v2 device record") from error
