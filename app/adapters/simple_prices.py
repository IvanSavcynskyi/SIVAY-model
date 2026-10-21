"""Import two-column crop/price CSV with explicit, table-level metadata."""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field

from app.canonical import digest
from app.domain import MarketPrice, Model, RuleSet


class SimplePriceImport(Model):
    csv: str = Field(min_length=1, max_length=2_000_000)
    currency: Literal["UAH", "USD", "EUR"]
    unit: Literal["t", "kg"]
    price_date: date
    published_at: AwareDatetime
    incoterm: Literal["EXW", "FCA", "CPT", "FOB", "DAP"]
    vat_included: bool
    vat_rate: float | None = Field(default=None, ge=0, le=1)
    region: str = "UA"
    source: str = Field(min_length=1)


def parse_simple_prices(payload: SimplePriceImport, rules: RuleSet) -> list[MarketPrice]:
    """Canonical variant id, displayed Ukrainian name or GAEZ parent code accepted.

    A parent quote is expanded explicitly into matching variants, retaining the
    unverified commodity specification; it never gains ranking eligibility.
    """
    if payload.price_date > payload.published_at.date():
        raise ValueError("Price observation date cannot be after publication")
    content = payload.csv.lstrip("\ufeff")
    if not content.strip():
        raise ValueError("Empty price CSV")
    first_line = content.splitlines()[0]
    delimiter = ";" if ";" in first_line else ","
    rows = list(csv.reader(io.StringIO(content), delimiter=delimiter))
    if not rows:
        raise ValueError("Empty price CSV")
    if [v.strip().casefold() for v in rows[0]] in (["crop", "price"], ["культура", "ціна"]):
        rows = rows[1:]
    result, seen = [], set()
    for line, row in enumerate(rows, 1):
        if not row or all(not v.strip() for v in row):
            continue
        if len(row) != 2:
            raise ValueError(f"CSV row {line}: expected crop,price")
        key = row[0].strip().casefold()
        variants = [crop for crop in rules.catalog if key in {
            crop.id.casefold(), crop.name_uk.casefold(), (crop.gaez_code or "").casefold(),
            (crop.parent_crop or "").casefold()} - {""}]
        if not variants:
            raise ValueError(f"CSV row {line}: unknown crop {row[0]!r}; use catalog id")
        try:
            value = float(row[1].strip().replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"CSV row {line}: invalid price") from exc
        for crop in variants:
            if crop.id in seen:
                raise ValueError(f"Duplicate or overlapping price for {crop.id}")
            seen.add(crop.id)
            result.append(MarketPrice(price_id="simple_" + digest({"metadata": payload.model_dump(mode="json"),
                "crop": crop.id, "value": value})[:24], crop=crop.id,
                commodity_spec=crop.commodity_spec or "unspecified", price_value=value,
                currency=payload.currency, unit=payload.unit, price_date=payload.price_date,
                published_at=payload.published_at, available_at=payload.published_at,
                location_type="region_avg", region=payload.region, incoterm=payload.incoterm,
                vat_included=payload.vat_included, vat_rate=payload.vat_rate,
                source=payload.source, source_reliability="low"))
    if not result:
        raise ValueError("No prices in CSV")
    return result
