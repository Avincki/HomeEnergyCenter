from __future__ import annotations

from pathlib import Path

import pytest

from energy_orchestrator.config.models import PricesConfig, PricesProvider
from energy_orchestrator.prices import (
    CsvPriceProvider,
    EntsoePriceProvider,
    PriceConfigurationError,
    create_price_provider,
)


def test_factory_returns_csv_provider_for_csv_config(tmp_path: Path) -> None:
    cfg = PricesConfig(provider=PricesProvider.CSV, csv_path=tmp_path / "p.csv")
    provider = create_price_provider(cfg)
    assert isinstance(provider, CsvPriceProvider)


def test_factory_returns_entsoe_provider_for_entsoe_config() -> None:
    cfg = PricesConfig(provider=PricesProvider.ENTSOE, api_key="k")
    provider = create_price_provider(cfg)
    assert isinstance(provider, EntsoePriceProvider)


def test_factory_raises_for_tibber() -> None:
    cfg = PricesConfig(provider=PricesProvider.TIBBER, api_key="k")
    with pytest.raises(PriceConfigurationError, match="Tibber"):
        create_price_provider(cfg)
