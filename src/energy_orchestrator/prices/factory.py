from __future__ import annotations

from energy_orchestrator.config.models import PricesConfig, PricesProvider
from energy_orchestrator.prices.base import PriceConfigurationError, PriceProvider
from energy_orchestrator.prices.csv_provider import CsvPriceProvider
from energy_orchestrator.prices.entsoe_provider import EntsoePriceProvider


def create_price_provider(config: PricesConfig) -> PriceProvider:
    """Build the right ``PriceProvider`` for the given config."""
    if config.provider is PricesProvider.CSV:
        return CsvPriceProvider(config)
    if config.provider is PricesProvider.ENTSOE:
        return EntsoePriceProvider(config, base_url=config.base_url)
    if config.provider is PricesProvider.TIBBER:
        raise PriceConfigurationError("Tibber provider is not implemented yet — use entsoe or csv")
    raise PriceConfigurationError(f"unknown price provider: {config.provider}")
