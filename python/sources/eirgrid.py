"""
EirGrid Smart Grid Dashboard adapter.

Fetches six API areas for a given date (all region=ALL):
  - windactual       → WIND_ACTUAL field     (MW, 15-min intervals)
  - generationactual → GEN_EXP field         (MW, 15-min intervals)
  - co2intensity     → CO2_INTENSITY field   (gCO2/kWh, 15-min intervals)
  - solaractual      → SOLAR_ACTUAL field    (MW, 15-min intervals)
  - demandactual     → SYSTEM_DEMAND field   (MW, 15-min intervals)
  - interconnection  → INTER_NET field       (MW, daily snapshot)

All reduced to daily averages before producing DailyReading instances.

Metrics produced:
  - wind_pct_of_generation_daily_avg   (%)
  - co2_intensity_daily_avg            (gCO2/kWh)
  - solar_pct_of_generation_daily_avg  (%)
  - demand_mw_daily_avg                (MW)
  - net_interconnection_mw_daily_avg   (MW, positive = net import to Ireland)

Raw file: data/raw/eirgrid/YYYY-MM-DD.json
  Contains all six API responses plus a fetch timestamp.
  Older raw files (missing solar/demand/interconnection keys) parse gracefully —
  those metrics are simply omitted until the file is re-fetched.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

import requests

from pipeline import DailyReading, atomic_write, raw_path

logger = logging.getLogger(__name__)

NAME = "eirgrid"
RAW_SUFFIX = ".json"

_BASE_URL = "https://www.smartgriddashboard.com/DashboardService.svc/data"
_TIMEOUT = 30  # seconds


def _eirgrid_date_range(d: date) -> tuple[str, str]:
    """Return (datefrom, dateto) strings in EirGrid's expected format."""
    day_str = d.strftime("%d-%b-%Y")
    return f"{day_str} 00:00", f"{day_str} 23:59"


def _fetch_area(area: str, d: date) -> dict:
    datefrom, dateto = _eirgrid_date_range(d)
    resp = requests.get(
        _BASE_URL,
        params={"area": area, "region": "ALL", "datefrom": datefrom, "dateto": dateto},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("ErrorMessage"):
        raise ValueError(f"EirGrid API error for area '{area}': {data['ErrorMessage']}")
    return data


class EirGridAdapter:
    name = NAME
    raw_suffix = RAW_SUFFIX

    def fetch(self, d: date) -> None:
        """Fetch all six EirGrid areas for date d.

        Saves all responses together in a single raw JSON file atomically.
        Raises on any network or API error.
        """
        logger.info("EirGrid: fetching data for %s", d)
        wind_data  = _fetch_area("windactual",       d)
        gen_data   = _fetch_area("generationactual", d)
        co2_data   = _fetch_area("co2intensity",     d)
        solar_data = _fetch_area("solaractual",      d)
        dem_data   = _fetch_area("demandactual",     d)
        inter_data = _fetch_area("interconnection",  d)

        raw = {
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            "date": d.isoformat(),
            "wind":          wind_data,
            "generation":    gen_data,
            "co2":           co2_data,
            "solar":         solar_data,
            "demand":        dem_data,
            "interconnection": inter_data,
        }
        path = raw_path(NAME, d, RAW_SUFFIX)
        atomic_write(path, json.dumps(raw, indent=2))
        logger.info("EirGrid: raw saved to %s", path)

    def parse(self, d: date) -> list[DailyReading]:
        """Parse the raw file for date d into DailyReading instances.

        Returns up to five readings. Older raw files missing the solar/demand/
        interconnection keys are handled gracefully — those metrics are simply
        omitted. If the core wind/co2 fields are corrupt, deletes the raw file
        so the next run re-fetches.
        """
        path = raw_path(NAME, d, RAW_SUFFIX)
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)

            # Core metrics (required — fail fast if missing)
            wind_pct = _compute_pct_of_generation(
                raw["wind"]["Rows"], raw["generation"]["Rows"]
            )
            co2_avg = _compute_avg(raw["co2"]["Rows"], "CO2_INTENSITY")

            # New metrics (optional — missing keys → no reading, not an error)
            solar_rows = raw.get("solar", {}).get("Rows", [])
            solar_pct = (
                _compute_pct_of_generation(solar_rows, raw["generation"]["Rows"])
                if solar_rows else None
            )

            demand_rows = raw.get("demand", {}).get("Rows", [])
            demand_avg = (
                _compute_avg(demand_rows, "SYSTEM_DEMAND")
                if demand_rows else None
            )

            inter_rows = raw.get("interconnection", {}).get("Rows", [])
            inter_avg = (
                _compute_avg(inter_rows, "INTER_NET")
                if inter_rows else None
            )

            readings: list[DailyReading] = []

            if wind_pct is not None:
                readings.append(DailyReading(
                    date=d, metric="wind_pct_of_generation_daily_avg",
                    value=round(wind_pct, 2), unit="%", source=NAME,
                ))
            else:
                logger.warning("EirGrid: could not compute wind %% for %s (no matched intervals)", d)

            if co2_avg is not None:
                readings.append(DailyReading(
                    date=d, metric="co2_intensity_daily_avg",
                    value=round(co2_avg, 1), unit="gCO2/kWh", source=NAME,
                ))
            else:
                logger.warning("EirGrid: could not compute CO2 avg for %s (no data)", d)

            if solar_pct is not None:
                readings.append(DailyReading(
                    date=d, metric="solar_pct_of_generation_daily_avg",
                    value=round(solar_pct, 2), unit="%", source=NAME,
                ))

            if demand_avg is not None:
                readings.append(DailyReading(
                    date=d, metric="demand_mw_daily_avg",
                    value=round(demand_avg, 1), unit="MW", source=NAME,
                ))

            if inter_avg is not None:
                readings.append(DailyReading(
                    date=d, metric="net_interconnection_mw_daily_avg",
                    value=round(inter_avg, 1), unit="MW", source=NAME,
                ))

            return readings

        except Exception as exc:
            logger.error("EirGrid: parse failed for %s (%s); deleting raw file to allow re-fetch", d, exc)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Reduction helpers
# ---------------------------------------------------------------------------


def _compute_pct_of_generation(source_rows: list[dict], gen_rows: list[dict]) -> float | None:
    """Compute a generation source as % of total generation, averaged across matched intervals.

    Works for any area whose rows contain (EffectiveTime, Value) pairs — windactual,
    solaractual, etc.
    """
    gen_by_time: dict[str, float] = {
        r.get("EffectiveTime"): r.get("Value")
        for r in gen_rows
        if r.get("EffectiveTime") is not None
        and r.get("Value") is not None
        and r.get("Value", 0) > 0
    }
    pcts: list[float] = []
    for r in source_rows:
        t = r.get("EffectiveTime")
        v = r.get("Value")
        if t is None or v is None:
            continue
        gen = gen_by_time.get(t)
        if gen is None or gen <= 0:
            continue
        pcts.append(v / gen * 100)

    return sum(pcts) / len(pcts) if pcts else None


# Keep the old name as an alias in case any external code references it.
_compute_wind_pct = _compute_pct_of_generation


def _compute_avg(rows: list[dict], field_name: str) -> float | None:
    """Return the simple average of all non-null values for the given field name."""
    vals = [r["Value"] for r in rows if r.get("FieldName") == field_name and r.get("Value") is not None]
    return sum(vals) / len(vals) if vals else None
