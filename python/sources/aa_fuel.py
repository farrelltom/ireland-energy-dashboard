"""
AA Ireland fuel price adapter.

Scrapes the AA Ireland fuel prices page for the current average Irish pump
prices for petrol and diesel.

Source URL: https://www.theaa.ie/aa-membership/fuel-prices/

The page contains a table with a "Pump price" row. Column order is:
  col 0: row label ("Pump price")
  col 1: Petrol (cents per litre, e.g. "181.00c")
  col 2: Diesel (cents per litre, e.g. "190.00c")

Prices are converted from cents to €/litre for the canonical store.

Metrics produced:
  - petrol_price_eur_per_litre   (€/litre)
  - diesel_price_eur_per_litre   (€/litre)

Raw file: data/raw/aa_fuel/YYYY-MM-DD.html
  Contains the full HTML page as fetched.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

from pipeline import DailyReading, atomic_write, raw_path

logger = logging.getLogger(__name__)

NAME = "aa_fuel"
RAW_SUFFIX = ".html"

_URL = "https://www.theaa.ie/aa-membership/fuel-prices/"
_TIMEOUT = 30  # seconds

# Matches prices like "181.00c" or "181c"
_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)c", re.IGNORECASE)


class AAFuelAdapter:
    name = NAME
    raw_suffix = RAW_SUFFIX

    def fetch(self, d: date) -> None:
        """Fetch the AA fuel prices page and save it as raw HTML.

        Raises on network error or non-200 response.
        """
        logger.info("AA Fuel: fetching data for %s", d)
        resp = requests.get(
            _URL,
            headers={"User-Agent": "ireland-energy-dashboard/1.0"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        path = raw_path(NAME, d, RAW_SUFFIX)
        atomic_write(path, resp.text)
        logger.info("AA Fuel: raw saved to %s", path)

    def parse(self, d: date) -> list[DailyReading]:
        """Parse the raw HTML for date d, returning petrol and diesel DailyReadings.

        If the raw file is missing, corrupt, or the price table cannot be
        found, deletes the raw file and raises so the next run re-fetches.
        """
        path = raw_path(NAME, d, RAW_SUFFIX)
        try:
            html = path.read_text(encoding="utf-8")
            petrol_eur, diesel_eur = _parse_pump_prices(html)

            return [
                DailyReading(
                    date=d,
                    metric="petrol_price_eur_per_litre",
                    value=round(petrol_eur, 4),
                    unit="€/litre",
                    source=NAME,
                ),
                DailyReading(
                    date=d,
                    metric="diesel_price_eur_per_litre",
                    value=round(diesel_eur, 4),
                    unit="€/litre",
                    source=NAME,
                ),
            ]

        except Exception as exc:
            logger.error(
                "AA Fuel: parse failed for %s (%s); deleting raw file to allow re-fetch",
                d,
                exc,
            )
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Parse helper
# ---------------------------------------------------------------------------


def _parse_pump_prices(html: str) -> tuple[float, float]:
    """Return (petrol_eur, diesel_eur) extracted from the AA pump price table.

    Raises ValueError if the table or 'Pump price' row cannot be found or
    if the price values cannot be parsed.

    The table header order is: label | Petrol | Diesel.
    Prices on the page are in cent/litre; returned values are in €/litre.
    """
    soup = BeautifulSoup(html, "html.parser")

    pump_row = None
    for td in soup.find_all("td"):
        if "Pump price" in td.get_text():
            pump_row = td.parent
            break

    if pump_row is None:
        raise ValueError("Could not find 'Pump price' row in AA fuel prices page")

    cells = pump_row.find_all("td")
    if len(cells) < 3:
        raise ValueError(f"Expected ≥3 cells in pump price row, got {len(cells)}: {cells}")

    petrol_cents = _extract_cents(cells[1].get_text())
    diesel_cents = _extract_cents(cells[2].get_text())

    return petrol_cents / 100, diesel_cents / 100


def _extract_cents(text: str) -> float:
    """Parse a price string like '181.00c' and return the float value in cents."""
    m = _PRICE_RE.search(text.strip())
    if m is None:
        raise ValueError(f"Could not parse price from: {text!r}")
    return float(m.group(1))
