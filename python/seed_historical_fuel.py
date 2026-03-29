"""
One-off script: seed historical AA Ireland fuel prices via the Wayback Machine.

Finds all 200-OK Wayback snapshots of the AA fuel prices page, fetches each
archived HTML, extracts pump prices using the existing parser, assigns the date
from the 'article:modified_time' meta tag (falling back to snapshot date), and
upserts into the canonical series CSV.

Usage (from repo root, with venv active):
    python python/seed_historical_fuel.py [--from YYYYMMDD] [--dry-run]

Defaults: --from 20250901  (last ~7 months of coverage)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Make sibling imports work when run as __main__ from any directory.
sys.path.insert(0, str(Path(__file__).parent))

from canonical import upsert
from pipeline import DailyReading
from sources.aa_fuel import _parse_pump_prices  # type: ignore[reportPrivateUsage]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("seed_historical_fuel")

_AA_URL = "https://www.theaa.ie/aa-membership/fuel-prices/"
_CDX_URL = "https://web.archive.org/cdx/search/cdx"
_WEB_URL = "https://web.archive.org/web/{timestamp}/{url}"
_SOURCE = "aa_fuel_wayback"
_REQUEST_HEADERS = {"User-Agent": "ireland-energy-dashboard-seed/1.0"}
_INTER_REQUEST_DELAY = 2.0  # seconds — be polite to Wayback


def _fetch_snapshots(from_date: str) -> list[dict]:
    """Return list of {timestamp, statuscode} dicts from Wayback CDX API."""
    logger.info("Querying Wayback CDX for snapshots from %s …", from_date)
    resp = requests.get(
        _CDX_URL,
        params={
            "url": _AA_URL,
            "output": "json",
            "fl": "timestamp,statuscode",
            "filter": "statuscode:200",
            "from": from_date,
            "to": datetime.now(tz=timezone.utc).strftime("%Y%m%d"),
            "collapse": "timestamp:8",  # at most one capture per calendar day
        },
        headers=_REQUEST_HEADERS,
        timeout=40,
    )
    resp.raise_for_status()
    rows = resp.json()
    if len(rows) <= 1:  # header row only
        return []
    header, *data = rows
    return [dict(zip(header, row)) for row in data]


def _extract_modified_date(soup: BeautifulSoup) -> date | None:
    """Return the article:modified_time date from the page meta tag, or None."""
    tag = soup.find("meta", property="article:modified_time")
    if tag is None:
        return None
    content = tag.get("content", "").strip()
    if not content:
        return None
    try:
        dt = datetime.fromisoformat(content)
        return dt.date()
    except ValueError:
        logger.warning("Could not parse article:modified_time: %r", content)
        return None


def _snapshot_to_date(timestamp: str) -> date:
    """Convert a 14-digit Wayback timestamp to a date (UTC)."""
    return datetime.strptime(timestamp[:8], "%Y%m%d").date()


def _fetch_and_parse(snapshot: dict) -> tuple[date, float, float] | None:
    """Fetch a single Wayback snapshot and return (reading_date, petrol, diesel).

    Returns None if fetching or parsing fails (logged as warning, not raised).
    """
    ts = snapshot["timestamp"]
    archive_url = _WEB_URL.format(timestamp=ts, url=_AA_URL)
    logger.info("Fetching snapshot %s …", archive_url)

    try:
        resp = requests.get(archive_url, headers=_REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Could not fetch snapshot %s: %s", ts, exc)
        return None

    html = resp.text
    try:
        petrol_eur, diesel_eur = _parse_pump_prices(html)
    except (ValueError, AttributeError) as exc:
        logger.warning("Could not parse prices from snapshot %s: %s", ts, exc)
        return None

    soup = BeautifulSoup(html, "html.parser")
    reading_date = _extract_modified_date(soup) or _snapshot_to_date(ts)
    logger.info(
        "  → %s  petrol=€%.3f  diesel=€%.3f",
        reading_date,
        petrol_eur,
        diesel_eur,
    )
    return reading_date, petrol_eur, diesel_eur


def _readings_from_result(
    reading_date: date, petrol_eur: float, diesel_eur: float
) -> list[DailyReading]:
    return [
        DailyReading(
            date=reading_date,
            metric="petrol_price_eur_per_litre",
            value=round(petrol_eur, 4),
            unit="€/litre",
            source=_SOURCE,
        ),
        DailyReading(
            date=reading_date,
            metric="diesel_price_eur_per_litre",
            value=round(diesel_eur, 4),
            unit="€/litre",
            source=_SOURCE,
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from",
        dest="from_date",
        default="20250901",
        metavar="YYYYMMDD",
        help="Earliest Wayback snapshot to include (default: 20250901)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be upserted but do not write to disk.",
    )
    args = parser.parse_args()

    snapshots = _fetch_snapshots(args.from_date)
    if not snapshots:
        logger.info("No snapshots found. Nothing to seed.")
        return

    logger.info("Found %d snapshot(s) to process.", len(snapshots))

    all_readings: list[DailyReading] = []
    seen_dates: set[date] = set()

    for i, snap in enumerate(snapshots):
        result = _fetch_and_parse(snap)
        if result is None:
            continue
        reading_date, petrol_eur, diesel_eur = result
        if reading_date in seen_dates:
            logger.info("  (duplicate date %s — skipping)", reading_date)
        else:
            seen_dates.add(reading_date)
            all_readings.extend(_readings_from_result(reading_date, petrol_eur, diesel_eur))

        if i < len(snapshots) - 1:
            time.sleep(_INTER_REQUEST_DELAY)

    if not all_readings:
        logger.info("No readings extracted. Nothing to write.")
        return

    logger.info("Extracted %d reading(s) across %d date(s).", len(all_readings), len(seen_dates))

    if args.dry_run:
        for r in all_readings:
            print(f"  DRY-RUN  {r.date}  {r.metric}  {r.value}")
        return

    upsert(all_readings)
    logger.info("Done. Upserted %d reading(s) into canonical store.", len(all_readings))


if __name__ == "__main__":
    main()
