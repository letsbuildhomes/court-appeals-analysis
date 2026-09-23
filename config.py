"""Shared paths and source settings for the court-PDF pipeline."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "in"
LISTINGS_DIR = RAW_DIR / "listings"
PDF_DIR = RAW_DIR / "pdfs"
DB_PATH = ROOT / "data" / "archive.duckdb"

SOURCE_URL = (
    "https://www.vtcourts.gov/opinions-decisions"
    "?f%5B0%5D=court_division_opinions_library_%3A3"
)
SOURCE_HOST = "www.vtcourts.gov"
USER_AGENT = "data-court-review/0.1 (research archive; sequential requests)"

DEFAULT_PDF_LIMIT = 5
REQUEST_DELAY_SECONDS = 0.5
BULK_REQUEST_DELAY_SECONDS = 1.5
BULK_REQUEST_JITTER_SECONDS = 0.5
MAX_PDF_BYTES = 100 * 1024 * 1024
