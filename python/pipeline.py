"""
Core types, shared utilities, and the SourceAdapter contract.

All adapters must implement SourceAdapter. The orchestrator (main.py) calls
is_raw_valid() before dispatching fetch(), then calls parse() to collect
DailyReading instances, which are passed in bulk to canonical.upsert().
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

DATA_DIR = Path(__file__).parent.parent / "data"


@dataclass(frozen=True)
class DailyReading:
    """One canonical data point: a single scalar value for a metric on a given date.

    - date:   calendar date the value represents (not the fetch timestamp)
    - metric: fully qualified name encoding the aggregation method and unit,
              e.g. "renewable_pct_daily_avg" or "petrol_price_eur_per_litre"
    - value:  the reduced scalar (adapter is responsible for reducing sub-daily
              series to a single daily value before constructing this)
    - unit:   human-readable unit string, e.g. "%" or "€/litre"
    - source: adapter name, e.g. "eirgrid" or "aa_fuel"
    """

    date: date
    metric: str
    value: float
    unit: str
    source: str


class SourceAdapter(Protocol):
    """Interface every source adapter must satisfy.

    Adapters are responsible for:
      - writing raw files atomically via atomic_write()
      - reducing sub-daily source data to one DailyReading per metric
      - deleting their own raw file if parse() fails on it (so the next run
        re-fetches rather than looping on a corrupt file)
    """

    name: str
    raw_suffix: str  # e.g. ".json" or ".html"

    def fetch(self, d: date) -> None:
        """Fetch source data for date d and write it to raw_path(self.name, d, self.raw_suffix).

        Raises on network or I/O failure. Caller catches and continues.
        """
        ...

    def parse(self, d: date) -> list[DailyReading]:
        """Read the raw file for date d, return one DailyReading per metric.

        If the raw file is corrupt or unparseable, delete it (to unblock
        re-fetch on the next run) and raise. Caller catches and continues.
        """
        ...


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def raw_path(source: str, d: date, suffix: str) -> Path:
    return DATA_DIR / "raw" / source / f"{d.isoformat()}{suffix}"


def parsed_path(source: str, d: date) -> Path:
    return DATA_DIR / "parsed" / source / f"{d.isoformat()}.json"


# ---------------------------------------------------------------------------
# Idempotency gate
# ---------------------------------------------------------------------------


def is_raw_valid(source: str, d: date, suffix: str) -> bool:
    """Return True if a non-empty raw file exists for this source and date.

    A zero-byte or missing file is treated as invalid. A non-zero file that
    is nonetheless corrupt will fail at parse() time; adapters are expected
    to delete it there so the next run re-fetches.
    """
    path = raw_path(source, d, suffix)
    return path.exists() and path.stat().st_size > 0


# ---------------------------------------------------------------------------
# Atomic file write
# ---------------------------------------------------------------------------


def atomic_write(path: Path, content: str | bytes) -> None:
    """Write content to path atomically via a sibling temp file + os.replace().

    Guarantees that readers never see a partial write. Cleans up the temp
    file if the write fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_binary = isinstance(content, bytes)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb" if is_binary else "w", encoding=None if is_binary else "utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
