"""
Energia electricity tariff adapter.

Scrapes the Energia "Our Tariffs" page for the Standard 24-hour electricity
unit rate and annual standing charge.

Source URL: https://www.energia.ie/about-energia/our-tariffs

The page contains a visible text table with rows like:
  "Standard 24hr unit price  42.65  39.13"  (incl VAT | excl VAT, cent/kWh)
  "Standing charge 24 hour urban per year  €265.01"

We always record the incl-VAT figures — the numbers a typical customer sees
on their bill.

Plan captured:   "Standard 24hr"
Standing charge: urban rate only (consistent, lower of the two)

Metrics produced (into tariffs.csv via upsert_tariffs):
  supplier:                    Energia
  plan:                        Standard 24hr
  unit_rate_eur_per_kwh:       e.g. 0.4265
  standing_charge_eur_per_year: e.g. 265.01

Raw file: data/raw/energia/YYYY-MM-DD.html
  Contains the full tariff page HTML as fetched.
"""
from __future__ import annotations

import logging
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from pipeline import TariffAdapter, atomic_write, raw_path

logger = logging.getLogger(__name__)

NAME = "energia"
RAW_SUFFIX = ".html"
SUPPLIER = "Energia"
PLAN = "Standard 24hr"

_URL = "https://www.energia.ie/about-energia/our-tariffs"
_TIMEOUT = 30

# Matches "Standard 24hr unit price  42.65  39.13" — we capture the incl-VAT value.
_UNIT_RATE_RE = re.compile(
    r"Standard 24hr unit price\s+([\d.]+)\s+[\d.]+",
    re.IGNORECASE,
)

# Matches "Standing charge 24 hour urban per year  €265.01"
_STANDING_RE = re.compile(
    r"Standing charge 24 hour urban per year\s+€([\d.]+)",
    re.IGNORECASE,
)


class EnergiaAdapter:
    name = NAME
    raw_suffix = RAW_SUFFIX

    def fetch(self, d: date) -> None:
        logger.info("Energia: fetching tariff page for %s", d)
        resp = requests.get(
            _URL,
            headers={"User-Agent": "ireland-energy-dashboard/1.0"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        path = raw_path(NAME, d, RAW_SUFFIX)
        atomic_write(path, resp.text)
        logger.info("Energia: raw saved to %s", path)

    def parse(self, d: date) -> list[dict]:
        path = raw_path(NAME, d, RAW_SUFFIX)
        try:
            html = path.read_text(encoding="utf-8")
            unit_rate_eur, standing_eur = _parse_tariff(html)
            logger.info(
                "Energia: parsed %s — unit_rate=€%.4f/kWh standing=€%.2f/yr",
                d,
                unit_rate_eur,
                standing_eur,
            )
            return [
                {
                    "date": d.isoformat(),
                    "supplier": SUPPLIER,
                    "plan": PLAN,
                    "unit_rate_eur_per_kwh": str(round(unit_rate_eur, 4)),
                    "standing_charge_eur_per_year": str(round(standing_eur, 2)),
                    "source": NAME,
                }
            ]
        except Exception as exc:
            logger.error(
                "Energia: parse failed for %s (%s); deleting raw file to allow re-fetch",
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


def _parse_tariff(html: str) -> tuple[float, float]:
    """Return (unit_rate_eur_per_kwh, standing_charge_eur_per_year) from the tariff page.

    Raises ValueError if either value cannot be found.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    m_rate = _UNIT_RATE_RE.search(text)
    if m_rate is None:
        raise ValueError("Could not find 'Standard 24hr unit price' on Energia tariff page")
    unit_rate_cents = float(m_rate.group(1))

    m_standing = _STANDING_RE.search(text)
    if m_standing is None:
        raise ValueError("Could not find '24 hour urban' standing charge on Energia tariff page")
    standing_eur = float(m_standing.group(1))

    return unit_rate_cents / 100, standing_eur
