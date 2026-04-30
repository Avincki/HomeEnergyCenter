from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from energy_orchestrator.config.models import PricesConfig, PricesProvider
from energy_orchestrator.prices import (
    CsvPriceProvider,
    PriceConfigurationError,
    PriceFetchError,
    PriceParseError,
)


def _config(csv_path: Path | None) -> PricesConfig:
    return PricesConfig(provider=PricesProvider.CSV, csv_path=csv_path, area="BE")


def _write_csv(path: Path, rows: list[str], header: bool = True) -> None:
    lines: list[str] = []
    if header:
        lines.append("timestamp,consumption_eur_per_kwh,injection_eur_per_kwh")
    lines.extend(rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def test_reads_all_rows_when_window_covers_file(tmp_path: Path) -> None:
    csv_file = tmp_path / "prices.csv"
    _write_csv(
        csv_file,
        [
            "2026-04-30T00:00:00+00:00,0.25,0.05",
            "2026-04-30T01:00:00+00:00,0.23,0.04",
            "2026-04-30T02:00:00+00:00,0.18,-0.02",
        ],
    )
    async with CsvPriceProvider(_config(csv_file)) as provider:
        points = list(
            await provider.fetch_prices(
                datetime(2026, 4, 30, tzinfo=UTC),
                datetime(2026, 5, 1, tzinfo=UTC),
            )
        )
    assert [p.consumption_eur_per_kwh for p in points] == [0.25, 0.23, 0.18]
    assert [p.injection_eur_per_kwh for p in points] == [0.05, 0.04, -0.02]


async def test_window_filter_excludes_rows_outside_range(tmp_path: Path) -> None:
    csv_file = tmp_path / "prices.csv"
    _write_csv(
        csv_file,
        [
            "2026-04-29T23:00:00+00:00,0.30,0.10",  # before window
            "2026-04-30T00:00:00+00:00,0.25,0.05",  # in window
            "2026-04-30T01:00:00+00:00,0.23,0.04",  # in window
            "2026-04-30T02:00:00+00:00,0.18,-0.02",  # in window (end is exclusive)
            "2026-04-30T03:00:00+00:00,0.15,-0.05",  # at end (excluded)
        ],
    )
    async with CsvPriceProvider(_config(csv_file)) as provider:
        points = list(
            await provider.fetch_prices(
                datetime(2026, 4, 30, 0, tzinfo=UTC),
                datetime(2026, 4, 30, 3, tzinfo=UTC),
            )
        )
    assert len(points) == 3
    assert points[0].timestamp == datetime(2026, 4, 30, 0, tzinfo=UTC)
    assert points[-1].timestamp == datetime(2026, 4, 30, 2, tzinfo=UTC)


async def test_naive_timestamp_treated_as_utc(tmp_path: Path) -> None:
    csv_file = tmp_path / "prices.csv"
    _write_csv(csv_file, ["2026-04-30T00:00:00,0.25,0.05"])
    async with CsvPriceProvider(_config(csv_file)) as provider:
        points = list(
            await provider.fetch_prices(
                datetime(2026, 4, 30, tzinfo=UTC),
                datetime(2026, 5, 1, tzinfo=UTC),
            )
        )
    assert len(points) == 1
    assert points[0].timestamp.tzinfo is UTC


async def test_missing_file_raises_fetch_error(tmp_path: Path) -> None:
    async with CsvPriceProvider(_config(tmp_path / "does-not-exist.csv")) as provider:
        with pytest.raises(PriceFetchError, match="not found"):
            await provider.fetch_prices(
                datetime(2026, 4, 30, tzinfo=UTC),
                datetime(2026, 5, 1, tzinfo=UTC),
            )


async def test_missing_columns_raises_parse_error(tmp_path: Path) -> None:
    csv_file = tmp_path / "prices.csv"
    csv_file.write_text("timestamp,price\n2026-04-30T00:00:00+00:00,0.25\n", encoding="utf-8")
    async with CsvPriceProvider(_config(csv_file)) as provider:
        with pytest.raises(PriceParseError, match="columns"):
            await provider.fetch_prices(
                datetime(2026, 4, 30, tzinfo=UTC),
                datetime(2026, 5, 1, tzinfo=UTC),
            )


async def test_bad_timestamp_raises_parse_error(tmp_path: Path) -> None:
    csv_file = tmp_path / "prices.csv"
    _write_csv(csv_file, ["not-a-timestamp,0.25,0.05"])
    async with CsvPriceProvider(_config(csv_file)) as provider:
        with pytest.raises(PriceParseError, match="timestamp"):
            await provider.fetch_prices(
                datetime(2026, 4, 30, tzinfo=UTC),
                datetime(2026, 5, 1, tzinfo=UTC),
            )


async def test_non_numeric_price_raises_parse_error(tmp_path: Path) -> None:
    csv_file = tmp_path / "prices.csv"
    _write_csv(csv_file, ["2026-04-30T00:00:00+00:00,not-a-number,0.05"])
    async with CsvPriceProvider(_config(csv_file)) as provider:
        with pytest.raises(PriceParseError, match="non-numeric"):
            await provider.fetch_prices(
                datetime(2026, 4, 30, tzinfo=UTC),
                datetime(2026, 5, 1, tzinfo=UTC),
            )


def test_constructor_rejects_missing_csv_path() -> None:
    # PricesConfig validation already prevents this, but re-check at provider level
    # in case someone bypasses it.
    cfg = PricesConfig.model_construct(provider=PricesProvider.CSV, csv_path=None)
    with pytest.raises(PriceConfigurationError, match="csv_path"):
        CsvPriceProvider(cfg)
