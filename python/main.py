"""
Pipeline orchestrator.

Run with: python main.py [--date YYYY-MM-DD]

Default date is today (UTC). Pass --date for historical backfill.

Exit codes:
  0  — all stages succeeded (even if some sources failed to fetch/parse)
  1  — upsert, analytics, or render failed (canonical or output may be stale)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone

import analytics
import canonical
import render
from pipeline import DailyReading, is_raw_valid
from sources.aa_fuel import AAFuelAdapter
from sources.bge import BGEAdapter
from sources.eirgrid import EirGridAdapter
from sources.energia import EnergiaAdapter
from sources.sse import SSEAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def run(d: date) -> int:
    """Execute one full pipeline pass for date d. Returns an exit code."""
    adapters = [EirGridAdapter(), AAFuelAdapter()]
    readings: list[DailyReading] = []

    # --- Fetch + Parse: series data (per-source isolation) ---
    for adapter in adapters:
        # Fetch
        if not is_raw_valid(adapter.name, d, adapter.raw_suffix):
            logger.info("[%s] No valid raw file for %s — fetching", adapter.name, d)
            try:
                adapter.fetch(d)
            except Exception as exc:
                logger.warning("[%s] Fetch failed: %s — skipping source", adapter.name, exc)
                continue
        else:
            logger.info("[%s] Valid raw file exists for %s — skipping fetch", adapter.name, d)

        # Parse
        try:
            source_readings = adapter.parse(d)
            logger.info("[%s] Parsed %d reading(s) for %s", adapter.name, len(source_readings), d)
            readings.extend(source_readings)
        except Exception as exc:
            logger.warning("[%s] Parse failed: %s — continuing without this source", adapter.name, exc)

    if not readings:
        logger.warning("No readings collected for %s — upsert skipped", d)
    else:
        # --- Upsert (single atomic write) ---
        logger.info("Upserting %d reading(s) into canonical store", len(readings))
        try:
            canonical.upsert(readings)
        except Exception as exc:
            logger.error("Upsert failed: %s", exc)
            return 1

    # --- Fetch + Parse: tariff data ---
    tariff_adapters = [EnergiaAdapter(), BGEAdapter(), SSEAdapter()]
    tariff_rows: list[dict] = []

    for adapter in tariff_adapters:
        if not is_raw_valid(adapter.name, d, adapter.raw_suffix):
            logger.info("[%s] No valid raw file for %s — fetching", adapter.name, d)
            try:
                adapter.fetch(d)
            except Exception as exc:
                logger.warning("[%s] Fetch failed: %s — skipping source", adapter.name, exc)
                continue
        else:
            logger.info("[%s] Valid raw file exists for %s — skipping fetch", adapter.name, d)

        try:
            rows = adapter.parse(d)
            logger.info("[%s] Parsed %d tariff row(s) for %s", adapter.name, len(rows), d)
            tariff_rows.extend(rows)
        except Exception as exc:
            logger.warning("[%s] Parse failed: %s — continuing without this source", adapter.name, exc)

    if tariff_rows:
        try:
            canonical.upsert_tariffs(tariff_rows)
        except Exception as exc:
            logger.error("Tariff upsert failed: %s", exc)
            return 1

    # --- Analytics ---
    logger.info("Running analytics")
    try:
        analytics.run()
    except Exception as exc:
        logger.error("Analytics failed: %s", exc)
        return 1

    # --- Render ---
    logger.info("Rendering index.html")
    try:
        render.run()
    except Exception as exc:
        logger.error("Render failed: %s", exc)
        return 1

    logger.info("Pipeline complete for %s", d)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Ireland Energy Dashboard pipeline")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=datetime.now(tz=timezone.utc).date(),
        help="Date to collect data for (YYYY-MM-DD). Defaults to today UTC.",
    )
    args = parser.parse_args()
    sys.exit(run(args.date))


if __name__ == "__main__":
    main()
