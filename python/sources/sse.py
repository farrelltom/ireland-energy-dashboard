"""
SSE Airtricity electricity tariff adapter.

SSE serves its tariff tables exclusively as PDFs — there are no in-DOM rate
numbers.  This adapter:

  1. Loads the tariff page with a headless browser (JavaScript renders the
     accordion and reveals PDF download links).
  2. Saves the rendered HTML as the raw file (so is_raw_valid works normally).
  3. At parse time, extracts the best-discount 1YR-Elec PDF URL from the
     saved HTML, downloads the PDF in-memory, and reads rates via pdfplumber.

Why download the PDF at parse time (not at fetch time)?
  • Fetch responsibility is to capture state (the rendered HTML).
  • PDF download is fast (<1 s) and the URL comes from the HTML.
  • Keeping parse() pure-ish (reads raw → returns rows) means re-parsing
    the cached raw file without a browser is trivially re-runnable.

PDF column structure (Standard 1 Year Home Electricity plan):

  Columns (per row, 10 numbers):
    [0] Standard Ex.VAT   [1] Standard Inc.VAT   ← existing customer base rate
    [2] X% DD&eBill Ex.VAT [3] X% DD&eBill Inc.VAT ← new customer best rate
    [4-9] other discount / payment channel variants

  Standing charge row "Urban 24 hr":
    [0] Ex.VAT/day  [1] Inc.VAT/day  [2] Ex.VAT/yr  [3] Inc.VAT/yr  ← we want [3]

The adapter always picks the highest-discount `1YR-Elec-{N}.pdf` link on the
page, so it automatically tracks SSE promotional changes.

Two rows produced per scrape:
  customer_type=existing  — standard undiscounted rate, Inc.VAT
  customer_type=new       — best DD+eBill discount, Inc.VAT

All rates incl. VAT, euro.  Running rates as at the scrape date (UTC).

Source URL: https://www.sseairtricity.com/ie/home/help-centre/our-tariffs/
Raw file:   data/raw/sse/YYYY-MM-DD.html
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from pipeline import atomic_write, raw_path
from sources.browser import fetch_rendered

logger = logging.getLogger(__name__)

NAME = "sse"
RAW_SUFFIX = ".html"
SUPPLIER = "SSE Airtricity"
PLAN = "Standard 24hr"

_TARIFF_PAGE = "https://www.sseairtricity.com/ie/home/help-centre/our-tariffs/"
_PDF_BASE = "https://www.sseairtricity.com"

# Matches "1YR-Elec-25.pdf", "1YR-Elec-35.pdf", etc.
_PDF_URL_RE = re.compile(r"/[^\"']*1YR-Elec-(\d+)\.pdf", re.IGNORECASE)

# Matches the 24 Hour Meter unit rate row (10 numbers on one line).
_UNIT_ROW_RE = re.compile(
    r"24 Hour Meter.*?\n([\d.\s]+)\n", re.DOTALL | re.IGNORECASE
)

# Matches the Urban 24hr standing charge row.
_STANDING_RE = re.compile(
    r"Urban 24 hr\s+([\d.]+)\s+([\d.]+)\s+€\s*([\d.]+)\s+€\s*([\d.]+)",
    re.IGNORECASE,
)

# Playwright selectors — SSE-specific.
_COOKIE_SELECTOR = "#onetrust-accept-btn-handler"
_ACCORDION_SELECTOR = "button:has-text('Current offers')"
_WAIT_FOR_SELECTOR = "a[href*='1YR-Elec']"
_CONTENT_ASSERTION = "1YR-Elec"

_TIMEOUT_S = 30


class SSEAdapter:
    name = NAME
    raw_suffix = RAW_SUFFIX

    def fetch(self, d: date) -> None:
        """Render the SSE tariff page and save the HTML as the raw file."""
        logger.info("SSE: rendering tariff page for %s", d)
        html = fetch_rendered(
            _TARIFF_PAGE,
            pre_click=_COOKIE_SELECTOR,
            click=_ACCORDION_SELECTOR,
            wait_for=_WAIT_FOR_SELECTOR,
            content_assertion=_CONTENT_ASSERTION,
        )
        path = raw_path(NAME, d, RAW_SUFFIX)
        atomic_write(path, html)
        logger.info("SSE: raw HTML saved to %s (%d chars)", path, len(html))

    def parse(self, d: date) -> list[dict]:
        """Parse the saved raw HTML, download the best PDF, return tariff rows."""
        path = raw_path(NAME, d, RAW_SUFFIX)
        try:
            html = path.read_text(encoding="utf-8")
            existing_c, new_c, discount_pct, standing_eur = _extract_rates(html)
            logger.info(
                "SSE: parsed %s — existing=%.2fc/kWh new=%.2fc/kWh (%d%% off) standing=€%.2f/yr",
                d, existing_c, new_c, discount_pct, standing_eur,
            )
            common = {
                "date": d.isoformat(),
                "supplier": SUPPLIER,
                "plan": PLAN,
                "standing_charge_eur_per_year": str(round(standing_eur, 2)),
                "source": NAME,
                "source_url": _TARIFF_PAGE,
                "source_type": "pdf",
            }
            return [
                {
                    **common,
                    "customer_type": "existing",
                    "unit_rate_eur_per_kwh": str(round(existing_c / 100, 4)),
                    "discount_pct": "",
                },
                {
                    **common,
                    "customer_type": "new",
                    "unit_rate_eur_per_kwh": str(round(new_c / 100, 4)),
                    "discount_pct": str(discount_pct),
                },
            ]
        except Exception as exc:
            logger.error(
                "SSE: parse failed for %s (%s); deleting raw file to allow re-fetch",
                d, exc,
            )
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def _best_pdf_url(html: str) -> tuple[str, int]:
    """Return (absolute_pdf_url, discount_pct) for the highest-discount 1YR-Elec PDF found.

    Raises ValueError if no matching PDF link is found.
    """
    soup = BeautifulSoup(html, "html.parser")
    best_pct = -1
    best_href = ""
    for tag in soup.find_all("a", href=_PDF_URL_RE):
        href = tag["href"]
        m = _PDF_URL_RE.search(href)
        if m:
            pct = int(m.group(1))
            if pct > best_pct:
                best_pct = pct
                best_href = href

    if not best_href:
        raise ValueError(
            "SSE: no '1YR-Elec-*.pdf' links found in rendered HTML — "
            "page structure may have changed."
        )

    # Ensure absolute URL.
    if best_href.startswith("http"):
        return best_href, best_pct
    return _PDF_BASE + best_href, best_pct


def _download_pdf(url: str) -> bytes:
    """Download *url* and return the bytes.  Raises on HTTP errors."""
    resp = requests.get(
        url,
        headers={"User-Agent": "ireland-energy-dashboard/1.0"},
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.content


def _parse_pdf(pdf_bytes: bytes) -> tuple[float, float, float]:
    """Parse a SSE 1YR-Elec PDF and return (existing_c, new_c, standing_eur).

    existing_c  — Standard Inc.VAT unit rate (cent/kWh)
    new_c       — best DD+eBill Inc.VAT unit rate (cent/kWh)
    standing_eur — Urban 24hr annual standing charge Inc.VAT (€/yr)
    """
    try:
        import pdfplumber  # noqa: PLC0415  (deferred import — see module docstring)
    except ImportError as exc:
        raise RuntimeError(
            "pdfplumber is not installed.  Run: pip install pdfplumber"
        ) from exc

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(
            pg.extract_text() or "" for pg in pdf.pages
        )

    # --- Unit rates ---
    m = _UNIT_ROW_RE.search(text)
    if not m:
        raise ValueError(
            "SSE PDF: could not find '24 Hour Meter' rate row. "
            "PDF layout may have changed."
        )
    nums = [float(x) for x in m.group(1).split()]
    if len(nums) < 4:
        raise ValueError(
            f"SSE PDF: expected ≥4 numbers in '24 Hour Meter' row, got {nums}"
        )
    existing_c = nums[1]  # Standard Inc.VAT
    new_c = nums[3]       # best DD+eBill Inc.VAT (highest discount plan)

    # --- Standing charge ---
    m2 = _STANDING_RE.search(text)
    if not m2:
        raise ValueError(
            "SSE PDF: could not find 'Urban 24 hr' standing charge row. "
            "PDF layout may have changed."
        )
    standing_eur = float(m2.group(4))  # Inc.VAT per year

    return existing_c, new_c, standing_eur


def _extract_rates(html: str) -> tuple[float, float, int, float]:
    """Top-level helper: find best PDF URL, download, parse, return all fields.

    Returns (existing_c, new_c, discount_pct, standing_eur).
    All rates are Inc.VAT; existing_c and new_c are in cent/kWh.
    """
    pdf_url, discount_pct = _best_pdf_url(html)
    logger.info("SSE: downloading tariff PDF (%d%% plan) from %s", discount_pct, pdf_url)
    pdf_bytes = _download_pdf(pdf_url)
    existing_c, new_c, standing_eur = _parse_pdf(pdf_bytes)
    return existing_c, new_c, discount_pct, standing_eur
