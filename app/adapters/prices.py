"""Local price/FX imports with explicit publication-time boundaries.

No exchange rates, commodity aliases, bushel factors, or tax rates are guessed.
CSV data is validated before it can enter an immutable feature snapshot.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, TypeVar

from app.adapters.satellite import as_of_cutoff
from app.domain import ExchangeRate, MarketPrice

Record = TypeVar("Record", MarketPrice, ExchangeRate)


def filter_prices(
    records: Iterable[MarketPrice], as_of: date | datetime | str,
) -> list[MarketPrice]:
    cutoff = as_of_cutoff(as_of)
    return sorted((row.model_copy(deep=True) for row in records if (
        row.price_date <= cutoff.date()
        and row.published_at <= cutoff
        and (row.available_at is None or row.available_at <= cutoff)
    )), key=lambda row: (row.price_date, row.published_at, row.price_id))


def filter_exchange_rates(
    records: Iterable[ExchangeRate], as_of: date | datetime | str,
) -> list[ExchangeRate]:
    cutoff = as_of_cutoff(as_of)
    return sorted((row.model_copy(deep=True) for row in records if (
        row.date <= cutoff.date()
        and row.available_at is not None
        and row.available_at <= cutoff
    )), key=lambda row: (row.date, row.currency, row.available_at, row.source))


def _load_csv(path: str | Path, model: type[Record]) -> list[Record]:
    rows = []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV requires unique column names")
        for number, row in enumerate(reader, 2):
            try:
                rows.append(model.model_validate({
                    key: None if value is None or value.strip().lower() in {"", "null"}
                    else value.strip() for key, value in row.items()
                }))
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"Invalid {model.__name__} CSV row {number}: {exc}") from exc
    return rows


def load_prices_csv(path: str | Path, as_of: date | datetime | str) -> list[MarketPrice]:
    records = _load_csv(path, MarketPrice)
    if len({row.price_id for row in records}) != len(records):
        raise ValueError("Duplicate price_id in price CSV")
    return filter_prices(records, as_of)


def load_exchange_rates_csv(
    path: str | Path, as_of: date | datetime | str,
) -> list[ExchangeRate]:
    """Undated publication availability is excluded, including historical rates."""
    return filter_exchange_rates(_load_csv(path, ExchangeRate), as_of)
