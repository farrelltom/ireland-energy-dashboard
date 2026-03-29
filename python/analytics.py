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

from canonical import CANONICAL_PATH, read_series, read_tariffs, series_sha256
from pipeline import DATA_DIR, DailyReading, atomic_write

logger = logging.getLogger(__name__)

INSIGHTS_PATH = DATA_DIR / "insights" / "latest.json"

# EV cost comparison constants
_EV_KWH_PER_100KM = 18.0
_DEFAULT_ELECTRICITY_EUR_PER_KWH = 0.40  # fallback if no tariff data
_PETROL_L_PER_100KM = 6.5
_DIESEL_L_PER_100KM = 6.2  # fixed assumption
_ANNUAL_KWH = 4200  # typical Irish household, used for apples-to-apples supplier comparison — typical diesel car, keep in sync with template label


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

    # EV vs petrol vs diesel cost comparison
    petrol_points = by_metric.get("petrol_price_eur_per_litre", [])
    diesel_points = by_metric.get("diesel_price_eur_per_litre", [])
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
            "consumption_l_per_100km": _PETROL_L_PER_100KM,
        }
    if diesel_points:
        diesel_eur = diesel_points[-1][1]
        diesel_cost_100km = diesel_eur * _DIESEL_L_PER_100KM
        metrics_out["diesel_cost_eur_per_100km"] = {
            "latest_value": round(diesel_cost_100km, 2),
            "latest_date": diesel_points[-1][0].isoformat(),
            "unit": "€/100km",
            "consumption_l_per_100km": _DIESEL_L_PER_100KM,
        }

    # --- Page-level freshness: max latest_date across all metrics on the page ---
    page_latest_date = max(
        (m["latest_date"] for m in metrics_out.values() if "latest_date" in m),
        default=None,
    )

    # --- Headline: one sentence combining the two most impactful facts ---
    headline = _build_headline(by_metric, metrics_out)

    # --- What changed: pre-evaluated directional pills (domain semantics applied) ---
    changes = _build_changes(by_metric)

    # --- Per-chart insight sentences (simple threshold rules) ---
    chart_insights = _build_chart_insights(by_metric)

    # --- Supplier tariff comparison ---
    tariff_comparison = _build_tariff_comparison()

    _write({
        "series_csv_sha256": sha,
        "page_latest_date": page_latest_date,
        "headline": headline,
        "changes": changes,
        "chart_insights": chart_insights,
        "tariff_comparison": tariff_comparison,
        "metrics": metrics_out,
        "insights": insights,
    })
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


def _build_tariff_comparison() -> dict:
    """Build supplier comparison data from the latest tariff row per supplier.

    Returns a dict with:
      suppliers: list of dicts sorted by annual_cost_eur ascending
      cheapest:  slug of cheapest supplier (or None)
      annual_kwh_assumed: the household usage assumption used
    """
    rows = read_tariffs()
    if not rows:
        return {"suppliers": [], "cheapest": None, "annual_kwh_assumed": _ANNUAL_KWH}

    # Keep only the most recent row per supplier.
    # Tie-break: scraped sources beat "manual" on the same date.
    latest: dict[str, dict] = {}
    for row in rows:
        supplier = row["supplier"]
        existing = latest.get(supplier)
        if existing is None:
            latest[supplier] = row
        elif row["date"] > existing["date"]:
            latest[supplier] = row
        elif row["date"] == existing["date"] and existing.get("source") == "manual" and row.get("source") != "manual":
            latest[supplier] = row

    suppliers = []
    for supplier, row in latest.items():
        unit_rate = float(row["unit_rate_eur_per_kwh"])
        standing = float(row["standing_charge_eur_per_year"])
        annual_cost = round(unit_rate * _ANNUAL_KWH + standing, 2)
        suppliers.append({
            "supplier": supplier,
            "plan": row["plan"],
            "unit_rate_eur_per_kwh": unit_rate,
            "standing_charge_eur_per_year": standing,
            "annual_cost_eur": annual_cost,
            "as_of": row["date"],
        })

    suppliers.sort(key=lambda s: s["annual_cost_eur"])
    cheapest = suppliers[0]["supplier"] if suppliers else None

    return {
        "suppliers": suppliers,
        "cheapest": cheapest,
        "annual_kwh_assumed": _ANNUAL_KWH,
    }


def _unit_for_metric(series: list[DailyReading], metric: str) -> str:
    for r in series:
        if r.metric == metric:
            return r.unit
    return ""


def _build_headline(
    by_metric: dict[str, list[tuple[date, float]]],
    metrics_out: dict[str, dict],
) -> str:
    """One sentence combining the two most impactful current facts.

    Uses 'yesterday' and 'at current pump prices' to match the actual
    aggregation semantics (latest daily point, not a weekly aggregate).
    """
    ev = metrics_out.get("ev_cost_eur_per_100km")
    petrol = metrics_out.get("petrol_cost_eur_per_100km")
    wind_pts = by_metric.get("wind_pct_of_generation_daily_avg", [])

    parts: list[str] = []
    if ev and petrol and petrol["latest_value"] > 0:
        saving_pct = (petrol["latest_value"] - ev["latest_value"]) / petrol["latest_value"] * 100
        parts.append(
            f"EV home charging costs {saving_pct:.0f}% less per 100\u202fkm "
            f"than petrol at current pump prices"
        )
    if wind_pts:
        parts.append(
            f"wind supplied {wind_pts[-1][1]:.0f}% of Ireland\u2019s electricity yesterday"
        )
    def _cap(s: str) -> str:
        return s[:1].upper() + s[1:] if s else s

    if not parts:
        return ""
    if len(parts) == 1:
        return _cap(parts[0]) + "."
    return f"{_cap(parts[0])} \u2014 and {parts[1]}."


def _build_changes(by_metric: dict[str, list[tuple[date, float]]]) -> list[dict]:
    """Pre-evaluated directional change pills with domain semantics applied.

    Direction is one of 'good', 'bad', 'neutral'.
    The caller (template) does not need to know which direction is positive —
    that inversion is applied here for CO₂ and fuel prices.
    """
    changes: list[dict] = []

    def _add(
        label: str,
        points: list[tuple[date, float]],
        higher_is_good: bool,
        fmt_fn,
    ) -> None:
        wow = _week_over_week(points)
        if wow is None:
            return
        arrow = "\u2191" if wow >= 0 else "\u2193"
        direction = ("good" if wow >= 0 else "bad") if higher_is_good else ("bad" if wow >= 0 else "good")
        changes.append({"label": label, "delta_str": f"{arrow}\u202f{fmt_fn(wow)}", "direction": direction})

    wind_pts = by_metric.get("wind_pct_of_generation_daily_avg", [])
    if wind_pts:
        _add("Wind", wind_pts, higher_is_good=True, fmt_fn=lambda d: f"{abs(d):.0f}pp")

    solar_pts = by_metric.get("solar_pct_of_generation_daily_avg", [])
    if solar_pts:
        _add("Solar", solar_pts, higher_is_good=True, fmt_fn=lambda d: f"{abs(d):.1f}pp")

    co2_pts = by_metric.get("co2_intensity_daily_avg", [])
    if co2_pts:
        _add("CO\u2082 intensity", co2_pts, higher_is_good=False, fmt_fn=lambda d: f"{abs(d):.0f}\u202fgCO\u2082/kWh")

    petrol_pts = by_metric.get("petrol_price_eur_per_litre", [])
    if petrol_pts:
        _add("Petrol", petrol_pts, higher_is_good=False, fmt_fn=lambda d: f"\u20ac{abs(d):.3f}/L")

    diesel_pts = by_metric.get("diesel_price_eur_per_litre", [])
    if diesel_pts:
        _add("Diesel", diesel_pts, higher_is_good=False, fmt_fn=lambda d: f"\u20ac{abs(d):.3f}/L")

    return changes


def _build_chart_insights(by_metric: dict[str, list[tuple[date, float]]]) -> dict[str, str]:
    """One plain-English sentence per metric chart, generated from simple threshold rules.

    Thresholds are set to produce actionable statements, not to restate the plotted line.
    """
    out: dict[str, str] = {}

    wind_pts = by_metric.get("wind_pct_of_generation_daily_avg", [])
    if wind_pts:
        wow = _week_over_week(wind_pts)
        if wow is not None:
            if wow > 5:
                out["wind_pct_of_generation_daily_avg"] = (
                    "Wind share is notably higher than last week — grid CO\u2082 emissions are likely lower as a result."
                )
            elif wow < -5:
                out["wind_pct_of_generation_daily_avg"] = (
                    "Wind share has fallen compared to last week — the grid has leaned more on gas and other sources."
                )
            else:
                out["wind_pct_of_generation_daily_avg"] = (
                    f"Wind has been providing around {wind_pts[-1][1]:.0f}% of generation — broadly stable week on week."
                )
        else:
            out["wind_pct_of_generation_daily_avg"] = (
                f"Wind currently supplies {wind_pts[-1][1]:.0f}% of Irish electricity generation."
            )

    co2_pts = by_metric.get("co2_intensity_daily_avg", [])
    if co2_pts:
        wow = _week_over_week(co2_pts)
        if wow is not None:
            if wow > 20:
                out["co2_intensity_daily_avg"] = (
                    "CO\u2082 intensity is higher than last week, likely reflecting less renewable output."
                )
            elif wow < -20:
                out["co2_intensity_daily_avg"] = (
                    "CO\u2082 intensity is lower than last week — more renewable generation is reducing grid emissions."
                )
            else:
                out["co2_intensity_daily_avg"] = "Grid CO\u2082 intensity has been broadly stable week on week."
        else:
            out["co2_intensity_daily_avg"] = f"Grid CO\u2082 intensity is currently {co2_pts[-1][1]:.0f}\u202fgCO\u2082/kWh."

    petrol_pts = by_metric.get("petrol_price_eur_per_litre", [])
    if petrol_pts:
        wow = _week_over_week(petrol_pts)
        if wow is not None and abs(wow) >= 0.005:
            direction = "risen" if wow > 0 else "fallen"
            out["petrol_price_eur_per_litre"] = f"Pump prices have {direction} since last week."
        else:
            out["petrol_price_eur_per_litre"] = "Pump prices are unchanged from last week."

    solar_pts = by_metric.get("solar_pct_of_generation_daily_avg", [])
    if solar_pts:
        wow = _week_over_week(solar_pts)
        if wow is not None:
            if wow > 1:
                out["solar_pct_of_generation_daily_avg"] = "Solar generation has increased compared to last week."
            elif wow < -1:
                out["solar_pct_of_generation_daily_avg"] = "Solar output has dipped compared to last week."
            else:
                out["solar_pct_of_generation_daily_avg"] = (
                    f"Solar is providing around {solar_pts[-1][1]:.1f}% of generation — broadly unchanged."
                )
        else:
            out["solar_pct_of_generation_daily_avg"] = (
                f"Solar currently provides {solar_pts[-1][1]:.1f}% of electricity generation."
            )

    inter_pts = by_metric.get("net_interconnection_mw_daily_avg", [])
    if inter_pts:
        latest_inter = inter_pts[-1][1]
        if latest_inter > 50:
            out["net_interconnection_mw_daily_avg"] = (
                f"Ireland is drawing around {latest_inter:.0f}\u202fMW from interconnectors — a net importer yesterday."
            )
        elif latest_inter < -50:
            out["net_interconnection_mw_daily_avg"] = (
                f"Ireland exported around {abs(latest_inter):.0f}\u202fMW via interconnectors yesterday — surplus renewable supply."
            )
        else:
            out["net_interconnection_mw_daily_avg"] = "Interconnection flows were close to balanced yesterday."

    return out
