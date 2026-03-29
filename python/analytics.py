"""
Analytics layer.

Reads the canonical series, computes insights, and writes
data/insights/latest.json. This file includes the SHA-256 of the canonical
CSV so render.py can verify it was produced from the current state.

Computed insights:
  - Latest values for each tracked metric
  - Week-over-week deltas (7-day comparison)
  - EV vs petrol cost comparison (per 100km)
  - Renewable (wind) trend direction

Assumptions used in EV cost comparison:
  - EV consumption: 18 kWh/100km (typical mid-size EV in Ireland)
  - Electricity rate: fixed at €0.40/kWh (typical Irish home charging rate).
    This is a deliberate fixed assumption, not read from canonical data.
    Update _DEFAULT_ELECTRICITY_EUR_PER_KWH if a tariff source is added.
  - Petrol car consumption: 6.5 L/100km (WLTP-ish average)
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta

from canonical import CANONICAL_PATH, read_series, series_sha256
from pipeline import DATA_DIR, DailyReading, atomic_write

logger = logging.getLogger(__name__)

INSIGHTS_PATH = DATA_DIR / "insights" / "latest.json"

# EV cost comparison constants
_EV_KWH_PER_100KM = 18.0
_DEFAULT_ELECTRICITY_EUR_PER_KWH = 0.40  # fallback if no tariff data
_PETROL_L_PER_100KM = 6.5


def run() -> None:
    """Compute insights from the canonical series and write latest.json.

    Raises on failure. Caller (main.py) treats this as a pipeline error.
    """
    series = read_series()
    sha = series_sha256()

    if not series:
        logger.warning("Analytics: canonical series is empty — writing minimal insights")
        _write({"series_csv_sha256": sha, "metrics": {}, "insights": []})
        return

    # Index by metric → sorted list of (date, value)
    by_metric: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for r in series:
        by_metric[r.metric].append((r.date, r.value))
    for points in by_metric.values():
        points.sort(key=lambda x: x[0])

    metrics_out: dict[str, dict] = {}
    insights: list[str] = []

    # --- Latest values ---
    for metric, points in by_metric.items():
        latest_date, latest_val = points[-1]
        metrics_out[metric] = {
            "latest_value": latest_val,
            "latest_date": latest_date.isoformat(),
            "unit": _unit_for_metric(series, metric),
        }

        # Week-over-week delta
        wow = _week_over_week(points)
        if wow is not None:
            metrics_out[metric]["week_over_week_delta"] = round(wow, 4)
            metrics_out[metric]["week_over_week_pct_change"] = (
                round(wow / (latest_val - wow) * 100, 1) if (latest_val - wow) != 0 else None
            )

    # --- Narrative insights ---

    # Wind %
    wind_points = by_metric.get("wind_pct_of_generation_daily_avg", [])
    if wind_points:
        latest_wind = wind_points[-1][1]
        wow = _week_over_week(wind_points)
        if wow is not None:
            direction = "up" if wow > 0 else "down"
            insights.append(
                f"Wind was {latest_wind:.0f}% of generation yesterday, "
                f"{direction} {abs(wow):.0f} percentage points vs last week."
            )
        else:
            insights.append(f"Wind provided {latest_wind:.0f}% of Irish electricity generation yesterday.")

    # CO2 intensity
    co2_points = by_metric.get("co2_intensity_daily_avg", [])
    if co2_points:
        latest_co2 = co2_points[-1][1]
        insights.append(f"Grid CO₂ intensity averaged {latest_co2:.0f} gCO₂/kWh yesterday.")

    # Solar %
    solar_points = by_metric.get("solar_pct_of_generation_daily_avg", [])
    if solar_points:
        latest_solar = solar_points[-1][1]
        insights.append(f"Solar provided {latest_solar:.1f}% of electricity generation yesterday.")

    # Demand
    demand_points = by_metric.get("demand_mw_daily_avg", [])
    if demand_points:
        latest_demand = demand_points[-1][1]
        wind_pts = by_metric.get("wind_pct_of_generation_daily_avg", [])
        if wind_pts:
            wind_mw_approx = latest_demand * wind_pts[-1][1] / 100
            insights.append(
                f"System demand averaged {latest_demand:.0f} MW yesterday "
                f"(wind supplied roughly {wind_mw_approx:.0f} MW of that)."
            )
        else:
            insights.append(f"System demand averaged {latest_demand:.0f} MW yesterday.")

    # Interconnection
    inter_points = by_metric.get("net_interconnection_mw_daily_avg", [])
    if inter_points:
        latest_inter = inter_points[-1][1]
        if latest_inter > 0:
            insights.append(
                f"Ireland was a net importer of electricity yesterday, "
                f"drawing an average of {latest_inter:.0f} MW from interconnectors."
            )
        else:
            insights.append(
                f"Ireland was a net exporter of electricity yesterday, "
                f"sending an average of {abs(latest_inter):.0f} MW abroad via interconnectors."
            )

    # EV vs petrol cost comparison
    petrol_points = by_metric.get("petrol_price_eur_per_litre", [])
    if petrol_points:
        petrol_eur = petrol_points[-1][1]
        electricity_eur = _DEFAULT_ELECTRICITY_EUR_PER_KWH
        petrol_cost_100km = petrol_eur * _PETROL_L_PER_100KM
        ev_cost_100km = electricity_eur * _EV_KWH_PER_100KM
        saving_pct = (petrol_cost_100km - ev_cost_100km) / petrol_cost_100km * 100
        insights.append(
            f"EV (home charging at €{electricity_eur:.2f}/kWh) costs "
            f"€{ev_cost_100km:.2f}/100km vs €{petrol_cost_100km:.2f}/100km for petrol — "
            f"{saving_pct:.0f}% cheaper."
        )
        metrics_out["ev_cost_eur_per_100km"] = {
            "latest_value": round(ev_cost_100km, 2),
            "latest_date": petrol_points[-1][0].isoformat(),
            "unit": "€/100km",
            "electricity_rate_used": electricity_eur,
        }
        metrics_out["petrol_cost_eur_per_100km"] = {
            "latest_value": round(petrol_cost_100km, 2),
            "latest_date": petrol_points[-1][0].isoformat(),
            "unit": "€/100km",
        }

    _write({"series_csv_sha256": sha, "metrics": metrics_out, "insights": insights})
    logger.info("Analytics: wrote %s", INSIGHTS_PATH)


def _write(data: dict) -> None:
    atomic_write(INSIGHTS_PATH, json.dumps(data, indent=2, default=str))


def _week_over_week(points: list[tuple[date, float]]) -> float | None:
    """Return latest_value − value_7_days_ago, or None if no prior data."""
    if len(points) < 2:
        return None
    latest_date, latest_val = points[-1]
    target = latest_date - timedelta(days=7)
    # Find the closest prior point within a 3-day window of the target
    candidates = [(d, v) for d, v in points[:-1] if abs((d - target).days) <= 3]
    if not candidates:
        return None
    _, prior_val = min(candidates, key=lambda x: abs((x[0] - target).days))
    return latest_val - prior_val


def _unit_for_metric(series: list[DailyReading], metric: str) -> str:
    for r in series:
        if r.metric == metric:
            return r.unit
    return ""
