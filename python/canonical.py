"""
Canonical series store.

The canonical store is a single narrow/long CSV at data/canonical/series.csv.
Schema: date (ISO-8601), metric, value (float), unit, source.
Primary key: (date, metric, source) — enforced by upsert().

All reads and writes go through this module. The CSV is written atomically;
readers never see a partial state.
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date
from pathlib import Path

from pipeline import DATA_DIR, DailyReading, atomic_write

CANONICAL_PATH = DATA_DIR / "canonical" / "series.csv"

_FIELDNAMES = ["date", "metric", "value", "unit", "source"]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_series() -> list[DailyReading]:
    """Return all rows from the canonical CSV as DailyReading instances.

    Returns an empty list if the file does not exist yet.
    DailyReading.date is always a datetime.date object, not a string.
    """
    if not CANONICAL_PATH.exists():
        return []

    rows: list[DailyReading] = []
    with CANONICAL_PATH.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(
                DailyReading(
                    date=date.fromisoformat(row["date"]),
                    metric=row["metric"],
                    value=float(row["value"]),
                    unit=row["unit"],
                    source=row["source"],
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def _key(r: DailyReading) -> tuple[date, str, str]:
    return (r.date, r.metric, r.source)


def upsert(readings: list[DailyReading]) -> None:
    """Upsert readings into the canonical CSV in a single atomic write.

    For any (date, metric, source) key that already exists, the new value
    wins. Rows are sorted by (date, metric, source) before writing.

    This is called once per pipeline run with all successfully parsed readings.
    If this raises, the CSV is unchanged (atomic write guarantee).
    """
    existing = {_key(r): r for r in read_series()}
    for r in readings:
        existing[_key(r)] = r

    sorted_rows = sorted(existing.values(), key=_key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    for r in sorted_rows:
        writer.writerow(
            {
                "date": r.date.isoformat(),
                "metric": r.metric,
                "value": r.value,
                "unit": r.unit,
                "source": r.source,
            }
        )

    atomic_write(CANONICAL_PATH, buf.getvalue())


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def series_sha256() -> str:
    """Return the SHA-256 hex digest of the canonical CSV file.

    Returns an empty string if the file does not exist.
    Used by analytics.py to stamp latest.json, and by render.py to verify
    that latest.json was produced from the current canonical state.
    """
    if not CANONICAL_PATH.exists():
        return ""
    return hashlib.sha256(CANONICAL_PATH.read_bytes()).hexdigest()
