"""CSV-backed price provider — useful for development and offline testing.

Expected CSV format (header row required):

    timestamp,consumption_eur_per_kwh,injection_eur_per_kwh
    2026-04-30T00:00:00+00:00,0.25,0.05
    2026-04-30T01:00:00+00:00,0.23,0.04

Naive timestamps are interpreted as UTC. The provider returns whatever is in
the file as-is — no factor/offset is applied. Author your CSV with the prices
you want to test against.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from energy_orchestrator.config.models import PricesConfig
from energy_orchestrator.prices.base import (
    PriceConfigurationError,
    PriceFetchError,
    PriceParseError,
    PricePoint,
    PriceProvider,
)

_REQUIRED_COLUMNS = (
    "timestamp",
    "consumption_eur_per_kwh",
    "injection_eur_per_kwh",
)


class CsvPriceProvider(PriceProvider):
    def __init__(self, config: PricesConfig) -> None:
        super().__init__(config)
        if config.csv_path is None:
            raise PriceConfigurationError("csv_path required for csv provider")
        self.csv_path = Path(config.csv_path)

    async def fetch_prices(self, start: datetime, end: datetime) -> Sequence[PricePoint]:
        if not self.csv_path.exists():
            raise PriceFetchError(f"CSV file not found: {self.csv_path}")
        try:
            with self.csv_path.open(encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None or not set(_REQUIRED_COLUMNS).issubset(
                    reader.fieldnames
                ):
                    raise PriceParseError(
                        f"CSV must have columns {_REQUIRED_COLUMNS}, got {reader.fieldnames}"
                    )
                points: list[PricePoint] = []
                for row_num, row in enumerate(reader, start=2):
                    try:
                        ts = datetime.fromisoformat(row["timestamp"])
                    except ValueError as e:
                        raise PriceParseError(
                            f"row {row_num}: bad timestamp {row['timestamp']!r}: {e}"
                        ) from e
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    if not (start <= ts < end):
                        continue
                    try:
                        cons = float(row["consumption_eur_per_kwh"])
                        inj = float(row["injection_eur_per_kwh"])
                    except (TypeError, ValueError) as e:
                        raise PriceParseError(f"row {row_num}: non-numeric price: {e}") from e
                    points.append(
                        PricePoint(
                            timestamp=ts,
                            consumption_eur_per_kwh=cons,
                            injection_eur_per_kwh=inj,
                        )
                    )
        except OSError as e:
            raise PriceFetchError(f"could not read {self.csv_path}: {e}") from e
        return points
