from __future__ import annotations


class DeviceError(Exception):
    """Base for all device-communication errors."""


class DeviceConnectionError(DeviceError):
    """Network-level failure: refused, unreachable, DNS failure."""


class DeviceTimeoutError(DeviceConnectionError):
    """Request timed out before a response arrived."""


class DeviceProtocolError(DeviceError):
    """Response could not be parsed or did not match the expected schema."""


class DeviceConfigurationError(DeviceError):
    """The device is misconfigured (e.g., missing token, wrong API version)."""


class UnknownDeviceTypeError(DeviceError):
    """No client class is registered for the given config type."""
