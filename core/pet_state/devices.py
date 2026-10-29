"""Small fail-closed ledger for paired, read-only pet devices."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import DeviceStoreError, DeviceStoreWriteError

_SCHEMA = 1
_MAX_ACTIVE_DEVICES = 16
_MAX_LEDGER_BYTES = 64 * 1024
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECORD_FIELDS = {
    "device_id",
    "name",
    "principal_id",
    "agent_id",
    "token_hash",
    "created_at",
}


class _IssueResult(dict[str, Any]):
    """JSON-serializable one-time issue response whose ordinary repr is redacted."""

    def __repr__(self) -> str:
        return (
            "IssueResult(device_id={!r}, agent_id={!r}, credential=<redacted>)"
        ).format(self.get("device_id"), self.get("agent_id"))

    __str__ = __repr__


class _OneTimeCredential(str):
    """A JSON-compatible secret that stays redacted in ordinary formatted logs."""

    def __repr__(self) -> str:
        return "'<redacted>'"

    def __str__(self) -> str:
        return "<redacted>"

    def __format__(self, format_spec: str) -> str:
        return "<redacted>"


class DeviceStore:
    """Persist active device bindings and SHA-256 hashes of one-time credentials."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def issue(
        self,
        name: str,
        principal_id: str,
        agent_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Issue a high-entropy credential once, persisting its hash before returning it."""
        label = _required_text(name, "name", 64)
        principal = _required_text(principal_id, "principal_id", 128)
        agent = _required_text(agent_id, "agent_id", 64)
        created_at = _aware_iso(now)
        credential = secrets.token_urlsafe(32)
        record = {
            "device_id": "dev_" + uuid.uuid4().hex,
            "name": label,
            "principal_id": principal,
            "agent_id": agent,
            "token_hash": hashlib.sha256(credential.encode("ascii")).hexdigest(),
            "created_at": created_at,
        }

        with self._lock:
            data = self._read()
            if len(data["devices"]) >= _MAX_ACTIVE_DEVICES:
                raise DeviceStoreError("active device limit reached")
            data["devices"].append(record)
            self._write(data)

        return _IssueResult(
            device_id=record["device_id"],
            agent_id=agent,
            name=label,
            created_at=created_at,
            credential=_OneTimeCredential(credential),
        )

    def authenticate(self, credential: str) -> dict[str, str] | None:
        """Resolve a credential to its binding without returning credential material."""
        if not isinstance(credential, str) or not credential or len(credential) > 512:
            return None
        try:
            candidate = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            return None

        with self._lock:
            try:
                data = self._read()
            except DeviceStoreError:
                return None
            found: dict[str, str] | None = None
            for record in data["devices"]:
                matched = hmac.compare_digest(record["token_hash"], candidate)
                if matched:
                    found = {
                        "device_id": record["device_id"],
                        "agent_id": record["agent_id"],
                        "principal_id": record["principal_id"],
                    }
            return found

    def revoke(self, device_id: str) -> bool:
        """Remove an active binding; the credential stops working as soon as this succeeds."""
        if not isinstance(device_id, str) or not device_id:
            return False
        with self._lock:
            try:
                data = self._read()
            except DeviceStoreError:
                return False
            remaining = [item for item in data["devices"] if item["device_id"] != device_id]
            if len(remaining) == len(data["devices"]):
                return False
            data["devices"] = remaining
            try:
                self._write(data)
            except DeviceStoreWriteError:
                return False
            return True

    def list_devices(self) -> list[dict[str, str]]:
        """Return active, display-safe metadata with neither hashes nor principal IDs."""
        with self._lock:
            data = self._read()
            return [
                {
                    "device_id": item["device_id"],
                    "name": item["name"],
                    "principal_id": item["principal_id"],
                    "agent_id": item["agent_id"],
                    "created_at": item["created_at"],
                }
                for item in data["devices"]
            ]

    def _read(self) -> dict[str, Any]:
        try:
            size = self.path.stat().st_size
            if size > _MAX_LEDGER_BYTES:
                raise DeviceStoreError("device ledger is too large")
            with self.path.open("rb") as source:
                raw = source.read(_MAX_LEDGER_BYTES + 1)
            if len(raw) > _MAX_LEDGER_BYTES:
                raise DeviceStoreError("device ledger is too large")
        except FileNotFoundError:
            return {"schema": _SCHEMA, "devices": []}
        except DeviceStoreError:
            raise
        except OSError as exc:
            raise DeviceStoreError("device ledger is unreadable") from exc

        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise DeviceStoreError("device ledger is invalid") from exc
        _validate_ledger(value)
        return value

    def _write(self, data: dict[str, Any]) -> None:
        parent = self.path.parent
        temp_path: Path | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(
                data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".pet-devices-", suffix=".tmp", dir=parent, delete=False
            ) as target:
                temp_path = Path(target.name)
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            if os.name == "posix":
                os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
            temp_path = None
            if os.name == "posix":
                os.chmod(self.path, 0o600)
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise DeviceStoreWriteError("device ledger update failed") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _validate_ledger(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"schema", "devices"}:
        raise DeviceStoreError("device ledger schema is invalid")
    schema = value.get("schema")
    devices = value.get("devices")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != _SCHEMA:
        raise DeviceStoreError("device ledger schema is unsupported")
    if not isinstance(devices, list) or len(devices) > _MAX_ACTIVE_DEVICES:
        raise DeviceStoreError("device ledger device list is invalid")
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for record in devices:
        if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
            raise DeviceStoreError("device ledger record is invalid")
        text_limits = {
            "device_id": 64,
            "name": 64,
            "principal_id": 128,
            "agent_id": 64,
            "created_at": 64,
        }
        for field, limit in text_limits.items():
            item = record.get(field)
            if not isinstance(item, str) or not item.strip() or len(item) > limit:
                raise DeviceStoreError("device ledger record is invalid")
        token_hash = record.get("token_hash")
        if not isinstance(token_hash, str) or not _HEX_SHA256.fullmatch(token_hash):
            raise DeviceStoreError("device ledger credential hash is invalid")
        if record["device_id"] in seen_ids or token_hash in seen_hashes:
            raise DeviceStoreError("device ledger contains duplicate records")
        seen_ids.add(record["device_id"])
        seen_hashes.add(token_hash)
        try:
            created = datetime.fromisoformat(record["created_at"])
        except ValueError as exc:
            raise DeviceStoreError("device ledger timestamp is invalid") from exc
        if created.tzinfo is None or created.utcoffset() is None:
            raise DeviceStoreError("device ledger timestamp is invalid")


def _required_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    text = value.strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{field} has an invalid length")
    return text


def _aware_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return value.isoformat()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


__all__ = ["DeviceStore"]
