"""Errors for the read-only desktop pet state bridge."""

from __future__ import annotations


class PetStateError(Exception):
    """Base class for public pet-state bridge failures."""


class DeviceStoreError(PetStateError):
    """The device credential ledger could not be trusted or updated."""


class DeviceStoreWriteError(DeviceStoreError):
    """A device-ledger update did not reach durable storage."""


class BindingMismatchError(PetStateError):
    """The authenticated device is not bound to the configured agent."""
