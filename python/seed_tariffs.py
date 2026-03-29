"""
One-off script: manually seed electricity supplier tariff data.

Usage (from repo root, with venv active):
    python python/seed_tariffs.py [--dry-run]

Edit the TARIFFS list below to add or update entries.
Each entry will upsert into data/canonical/tariffs.csv.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from canonical import upsert_tariffs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("seed_tariffs")

# ---------------------------------------------------------------------------
# Edit this list to add historical or current tariff entries.
# unit_rate_eur_per_kwh: electricity unit rate in €/kWh
# standing_charge_eur_per_year: annual standing charge in €/year
# ---------------------------------------------------------------------------
TARIFFS: list[dict] = [
    {
        "date": date(2026, 3, 29).isoformat(),
        "supplier": "Energia",
        "plan": "Standard 24hr",
        "unit_rate_eur_per_kwh": "0.332",
        "standing_charge_eur_per_year": "310",
        "source": "manual",
    },
    # Add more rows here — e.g. from supplier websites or press releases:
    # {
    #     "date": date(2026, 3, 29).isoformat(),
    #     "supplier": "Electric Ireland",
    #     "plan": "Standard",
    #     "unit_rate_eur_per_kwh": "0.355",
    #     "standing_charge_eur_per_year": "330",
    #     "source": "manual",
    # },
    # {
    #     "date": date(2026, 3, 29).isoformat(),
    #     "supplier": "SSE Airtricity",
    #     "plan": "Standard",
    #     "unit_rate_eur_per_kwh": "0.347",
    #     "standing_charge_eur_per_year": "320",
    #     "source": "manual",
    # },
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print without writing.")
    args = parser.parse_args()

    if args.dry_run:
        for row in TARIFFS:
            print(
                f"  DRY-RUN  {row['date']}  {row['supplier']}  {row['plan']}"
                f"  rate={row['unit_rate_eur_per_kwh']}  standing={row['standing_charge_eur_per_year']}"
            )
        return

    upsert_tariffs(TARIFFS)
    logger.info("Seeded %d tariff row(s) into data/canonical/tariffs.csv", len(TARIFFS))


if __name__ == "__main__":
    main()
