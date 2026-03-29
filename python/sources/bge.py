"""
Bord Gáis Energy electricity tariff adapter.

Scrapes the BGE "Our Plans" page for Standard 24hr electricity unit rates
(both existing and new customer) and the annual electricity standing charge.

Source URL: https://www.bordgaisenergy.ie/home/our-plans

The page embeds a __NEXT_DATA__ JSON blob containing all plan data.  We
find electricity-only ("Single Fuel") plans with planType == "Flat"
(BGE's name for their standard single 24hr rate plan):

  electricityDetail.estimated.smartRates.oDay   → undiscounted rate (incl VAT)
  electricityDetail.estimated.smartRates.day     → discounted rate  (incl VAT)
  electricityDetail.discountBreakdown.day         → discount %, e.g. -30
  electricityDetail.estimated.standing            → annual electricity standing charge (incl VAT)

Two rows produced per scrape:
  customer_type=existing  — undiscounted base rate (oDay, cent/kWh)
  customer_type=new       — best advertised Single Fuel new-customer rate

BGE states "All prices are inclusive of VAT" on the page.

Raw file: data/raw/bge/YYYY-MM-DD.html
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date

import requests

from pipeline import TariffAdapter, atomic_write, raw_path

logger = logging.getLogger(__name__)

NAME = "bge"
RAW_SUFFIX = ".html"
SUPPLIER = "Bord Gáis Energy"
PLAN = "Standard 24hr"

_URL = "https://www.bordgaisenergy.ie/home/our-plans"
_TIMEOUT = 30

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


class BGEAdapter:
    name = NAME
    raw_suffix = RAW_SUFFIX

    def fetch(self, d: date) -> None:
        logger.info("BGE: fetching plans page for %s", d)
        resp = requests.get(
            _URL,
            headers={"User-Agent": "ireland-energy-dashboard/1.0"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        path = raw_path(NAME, d, RAW_SUFFIX)
        atomic_write(path, resp.text)
        logger.info("BGE: raw saved to %s", path)

    def parse(self, d: date) -> list[dict]:
        path = raw_path(NAME, d, RAW_SUFFIX)
        try:
            html = path.read_text(encoding="utf-8")
            existing_rate_eur, new_rate_eur, discount_pct, standing_eur = _parse_plans(html)
            logger.info(
                "BGE: parsed %s — existing=€%.4f/kWh new=€%.4f/kWh (%d%% off) standing=€%.2f/yr",
                d, existing_rate_eur, new_rate_eur, discount_pct, standing_eur,
            )
            common = {
                "date": d.isoformat(),
                "supplier": SUPPLIER,
                "plan": PLAN,
                "standing_charge_eur_per_year": str(round(standing_eur, 2)),
                "source": NAME,
                "source_url": _URL,
                "source_type": "json_embedded",
            }
            return [
                {
                    **common,
                    "customer_type": "existing",
                    "unit_rate_eur_per_kwh": str(round(existing_rate_eur, 4)),
                    "discount_pct": "",
                },
                {
                    **common,
                    "customer_type": "new",
                    "unit_rate_eur_per_kwh": str(round(new_rate_eur, 4)),
                    "discount_pct": str(discount_pct),
                },
            ]
        except Exception as exc:
            logger.error(
                "BGE: parse failed for %s (%s); deleting raw file to allow re-fetch",
                d,
                exc,
            )
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Parse helper (pure function — easy to test in isolation)
# ---------------------------------------------------------------------------


def _parse_plans(html: str) -> tuple[float, float, int, float]:
    """Return (existing_rate_eur, new_rate_eur, discount_pct, standing_eur).

    All rates are incl-VAT, in EUR/kWh.  Raises ValueError if required data
    cannot be found.  Searches the __NEXT_DATA__ JSON blob for Single Fuel,
    planType==Flat electricity detail entries.
    """
    m = _NEXT_DATA_RE.search(html)
    if m is None:
        raise ValueError("Could not find __NEXT_DATA__ JSON in BGE page")

    data = json.loads(m.group(1))

    flat_elec: list[dict] = []  # electricityDetail dicts for Flat plans

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            elec = obj.get("electricityDetail")
            if (
                isinstance(elec, dict)
                and elec.get("planType") == "Flat"
                and obj.get("fuelType") == "Single Fuel"
            ):
                flat_elec.append((elec, obj.get("fuelType", "")))
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)

    if not flat_elec:
        raise ValueError("No Single Fuel Flat electricity plans found in BGE __NEXT_DATA__")

    # Existing customer rate: the undiscounted base rate (oDay) — consistent
    # across all Flat plans at BGE.
    o_day_values = [
        e["estimated"]["smartRates"]["oDay"]
        for e, _ in flat_elec
        if isinstance(e.get("estimated"), dict)
        and isinstance(e["estimated"].get("smartRates"), dict)
        and e["estimated"]["smartRates"].get("oDay") is not None
    ]
    if not o_day_values:
        raise ValueError("Could not find oDay rate in BGE data")
    existing_cents = o_day_values[0]  # all Flat plans share same oDay

    standing_values = [
        e["estimated"]["standing"]
        for e, _ in flat_elec
        if isinstance(e.get("estimated"), dict)
        and e["estimated"].get("standing") is not None
    ]
    if not standing_values:
        raise ValueError("Could not find standing charge in BGE data")
    standing_eur = standing_values[0]

    # New customer rate: best (most negative) discount among New customer entries.
    new_entries = [
        e for e, _ in flat_elec
        if e.get("customerType") == "New"
        and isinstance(e.get("discountBreakdown"), dict)
        and isinstance(e["discountBreakdown"].get("day"), (int, float))
    ]
    if not new_entries:
        raise ValueError("No New customer Flat electricity plans found in BGE data")

    best = min(new_entries, key=lambda e: e["discountBreakdown"]["day"])
    discount_pct = abs(int(best["discountBreakdown"]["day"]))
    new_cents = best["estimated"]["smartRates"]["day"]

    return existing_cents / 100, new_cents / 100, discount_pct, standing_eur
