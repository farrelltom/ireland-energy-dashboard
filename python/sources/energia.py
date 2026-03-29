"""
Energia electricity tariff adapter.

Scrapes the Energia "Our Tariffs" page for the Standard 24-hour electricity
unit rate and annual standing charge.

Source URL: https://www.energia.ie/about-energia/our-tariffs

The page contains a 3-column table (label | incl VAT | excl VAT):
  "Standard 24hr unit price  42.65  39.13"   (cent/kWh)
  "With 30% discount         29.86  27.39"
  "Standing charge 24 hour urban per year  €265.01"

All rates are incl-VAT — the numbers a typical customer sees on their bill.

Two rows produced per scrape:
  customer_type=existing  — standard undiscounted rate (42.65c)
  customer_type=new       — discounted new-customer rate (29.86c, 30% off)

Standing charge: urban rate only (consistent, lower of the two).

Raw file: data/raw/energia/YYYY-MM-DD.html
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

# Matches "Standard 24hr unit price  42.65  39.13" — incl-VAT is first number.
_UNIT_RATE_RE = re.compile(
    r"Standard 24hr unit price\s+([\d.]+)\s+[\d.]+",
    re.IGNORECASE,
)

# Matches "With 30% discount  29.86  27.39" — captures discount % and incl-VAT rate.
_DISCOUNT_RE = re.compile(
    r"With\s+(\d+)%\s+discount\s+([\d.]+)\s+[\d.]+",
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
            existing_rate_eur, new_rate_eur, discount_pct, standing_eur = _parse_tariff(html)
            logger.info(
                "Energia: parsed %s — existing=€%.4f/kWh new=€%.4f/kWh (%d%% off) standing=€%.2f/yr",
                d, existing_rate_eur, new_rate_eur, discount_pct, standing_eur,
            )
            common = {
                "date": d.isoformat(),
                "supplier": SUPPLIER,
                "plan": PLAN,
                "standing_charge_eur_per_year": str(round(standing_eur, 2)),
                "source": NAME,
                "source_url": _URL,
                "source_type": "html",
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


def _parse_tariff(html: str) -> tuple[float, float, int, float]:
    """Return (existing_rate_eur, new_rate_eur, discount_pct, standing_eur).

    All rates are incl-VAT.  Raises ValueError if required values are missing.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    m_rate = _UNIT_RATE_RE.search(text)
    if m_rate is None:
        raise ValueError("Could not find 'Standard 24hr unit price' on Energia tariff page")
    existing_cents = float(m_rate.group(1))

    m_disc = _DISCOUNT_RE.search(text)
    if m_disc is None:
        raise ValueError("Could not find discount row on Energia tariff page")
    discount_pct = int(m_disc.group(1))
    new_cents = float(m_disc.group(2))

    m_standing = _STANDING_RE.search(text)
    if m_standing is None:
        raise ValueError("Could not find '24 hour urban' standing charge on Energia tariff page")
    standing_eur = float(m_standing.group(1))

    return existing_cents / 100, new_cents / 100, discount_pct, standing_eur
