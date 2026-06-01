"""Best-effort IP-based geolocation of the host running the app.

A Raspberry Pi has no GPS, so the only "where is this device" signal is its
public IP. This resolves that IP to an approximate lat/lon via a free, no-key
geolocation service. Accuracy is city-level (often several km off), so the
result is only useful as a *suggested* default for a coarse geofence — the
caller is expected to widen the radius accordingly and let the user refine it.

Pure and best-effort: any failure (network, non-2xx, bad JSON, missing or
out-of-range fields) returns ``None`` rather than raising, so a geolocation
hiccup never blocks the page that asked for the suggestion.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import structlog

logger = structlog.stdlib.get_logger(__name__)

# Free, no API key, HTTPS. Returns JSON with "latitude"/"longitude" (degrees).
# We also accept the "lat"/"lon" spelling so an alternate provider can be
# swapped in via ``url`` without code changes.
_DEFAULT_GEO_URL = "https://ipapi.co/json/"


async def detect_device_location(
    *, url: str = _DEFAULT_GEO_URL, timeout_s: float = 3.0
) -> tuple[float, float] | None:
    """Return ``(latitude, longitude)`` for this host's public IP, or ``None``.

    Never raises — a lookup failure is logged at INFO and yields ``None`` so the
    caller can silently fall back to leaving the field blank.
    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as resp,
        ):
            if resp.status >= 400:
                logger.info("device geolocation lookup failed", status=resp.status, url=url)
                return None
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        logger.info("device geolocation lookup error", error=str(e), url=url)
        return None

    if not isinstance(payload, dict):
        return None
    lat = _as_float(payload.get("latitude", payload.get("lat")))
    lon = _as_float(payload.get("longitude", payload.get("lon")))
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        logger.info("device geolocation out of range", lat=lat, lon=lon)
        return None
    logger.info("device geolocation detected", lat=lat, lon=lon)
    return lat, lon


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
