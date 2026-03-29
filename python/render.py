"""
Renderer.

Reads data/insights/latest.json, verifies its series_csv_sha256 matches the
current canonical CSV, then renders index.html from templates/index.html.jinja.

Raises if the sha256 check fails (stale insights) or if rendering fails.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from canonical import read_series, series_sha256, tariffs_sha256
from pipeline import DATA_DIR, atomic_write

logger = logging.getLogger(__name__)

INSIGHTS_PATH = DATA_DIR / "insights" / "latest.json"
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
OUTPUT_PATH = Path(__file__).parent.parent / "docs" / "index.html"


def run() -> None:
    """Verify insights freshness, then render index.html.

    Raises on sha256 mismatch or template error.
    """
    if not INSIGHTS_PATH.exists():
        raise FileNotFoundError(f"Insights file not found: {INSIGHTS_PATH}")

    with INSIGHTS_PATH.open(encoding="utf-8") as f:
        insights = json.load(f)

    current_series_sha = series_sha256()
    stored_series_sha = insights.get("series_csv_sha256", "")
    if current_series_sha != stored_series_sha:
        raise RuntimeError(
            f"Stale insights detected: latest.json was produced from a different "
            f"canonical series state (stored={stored_series_sha[:12]}\u2026, "
            f"current={current_series_sha[:12]}\u2026). "
            f"Re-run analytics before rendering."
        )

    current_tariffs_sha = tariffs_sha256()
    stored_tariffs_sha = insights.get("tariffs_csv_sha256", "")
    if current_tariffs_sha != stored_tariffs_sha:
        raise RuntimeError(
            f"Stale insights detected: latest.json was produced from a different "
            f"tariffs state (stored={stored_tariffs_sha[:12]}\u2026, "
            f"current={current_tariffs_sha[:12]}\u2026). "
            f"Re-run analytics before rendering."
        )

    # Read series exactly once, after the sha256 check, so both insights and
    # chart_data are derived from the same canonical state that produced the sha.
    series = read_series()
    chart_data = _build_chart_data(series)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("index.html.jinja")
    html = template.render(insights=insights, chart_data=chart_data)

    atomic_write(OUTPUT_PATH, html)
    logger.info("Renderer: wrote %s", OUTPUT_PATH)


def _build_chart_data(series: list) -> dict:
    """Build chart-ready data structures from a pre-loaded canonical series.

    Accepts the series already read by run() after the sha256 check, so both
    insights and chart_data are derived from the same canonical snapshot.

    Returns a dict of metric → {labels: [...], data: [...]} for Chart.js.
    Labels are ISO date strings; values are floats.
    Only the last 90 days of data are included per metric to keep the page lean.
    """
    from collections import defaultdict

    by_metric: dict[str, list[tuple]] = defaultdict(list)
    for r in series:
        by_metric[r.metric].append((r.date, r.value))

    chart_data: dict[str, dict] = {}
    for metric, points in by_metric.items():
        points.sort(key=lambda x: x[0])
        # Last 90 days
        points = points[-90:]
        chart_data[metric] = {
            "labels": [d.isoformat() for d, _ in points],
            "data": [v for _, v in points],
        }
    return chart_data
