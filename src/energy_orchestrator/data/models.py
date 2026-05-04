from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DecisionState(StrEnum):
    ON = "on"
    OFF = "off"


class OverrideMode(StrEnum):
    AUTO = "auto"
    FORCE_ON = "force_on"
    FORCE_OFF = "force_off"


class SourceName(StrEnum):
    SONNEN = "sonnen"
    CAR_CHARGER = "car_charger"
    P1_METER = "p1_meter"
    SMALL_SOLAR = "small_solar"
    LARGE_SOLAR = "large_solar"
    SOLAREDGE = "solaredge"
    PRICES = "prices"
    SOLAR_FORECAST = "solar_forecast"


class Reading(Base):
    """A single poll snapshot: numeric values from every reachable source."""

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    battery_soc_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    house_consumption_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    production_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    grid_feed_in_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    car_charger_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    p1_active_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    small_solar_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    large_solar_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    injection_price_eur_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    consumption_price_eur_per_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)


class Decision(Base):
    """One rule-engine outcome per tick. Includes manual-override metadata."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(8), nullable=False)
    rule_fired: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    state_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    override_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    forecast_end_soc_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class SourceStatus(Base):
    """Per-source health snapshot powering the debug-board health panel."""

    __tablename__ = "source_status"

    source_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
