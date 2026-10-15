"""Settings read from environment variables. Nothing secret is stored in the database or logs."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STUDYTOOL_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("STUDYTOOL_DB", DATA_DIR / "studytool.db"))
PDF_DIR = DATA_DIR / "pdfs"
PAGE_IMG_DIR = DATA_DIR / "pages"
EXPORT_DIR = DATA_DIR / "exports"

HOST = os.environ.get("STUDYTOOL_HOST", "127.0.0.1")  # loopback only by default
PORT = int(os.environ.get("STUDYTOOL_PORT", "8501"))

# AI provider. The API key is read by the SDK from GEMINI_API_KEY; never log it.
AI_PROVIDER = "gemini"
GEMINI_MODEL = os.environ.get("STUDYTOOL_GEMINI_MODEL", "gemini-3.8-flash")
# Estimated USD per 1M tokens. Costs shown in the UI are estimates only.
PRICE_IN_PER_M = float(os.environ.get("STUDYTOOL_PRICE_IN_PER_M", "0.30"))
PRICE_OUT_PER_M = float(os.environ.get("STUDYTOOL_PRICE_OUT_PER_M", "2.50"))
MAX_AI_CONCURRENCY = int(os.environ.get("STUDYTOOL_MAX_AI_CONCURRENCY", "4"))
AI_MAX_RETRIES = 4

# Bump when the logic changes so cached results are not reused.
EXTRACTOR_VERSION = "1"
SEGMENT_PROMPT_VERSION = "1"
GENERATE_PROMPT_VERSION = "1"

WORKER_HEARTBEAT_SECONDS = 5
WORKER_STALE_SECONDS = 30  # a worker silent this long is treated as dead

for _d in (DATA_DIR, PDF_DIR, PAGE_IMG_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
