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

# AI provider. The key is read from DEEPSEEK_API_KEY at call time; never log or store it.
AI_PROVIDER = "deepseek"
DEEPSEEK_BASE_URL = os.environ.get("STUDYTOOL_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("STUDYTOOL_DEEPSEEK_MODEL", "deepseek-flash")  # deepseek-flash reads page images
# Thinking mode is slower and costs more output tokens; off unless asked for.
DEEPSEEK_THINKING = os.environ.get("STUDYTOOL_DEEPSEEK_THINKING", "0") == "1"
AI_TIMEOUT_SECONDS = 180
AI_MAX_OUTPUT_TOKENS = 16000
# Estimated USD per 1M tokens (deepseek-flash peak-hour list price, Oct 2026). Estimates only.
PRICE_IN_PER_M = float(os.environ.get("STUDYTOOL_PRICE_IN_PER_M", "0.30"))
PRICE_IN_CACHED_PER_M = float(os.environ.get("STUDYTOOL_PRICE_IN_CACHED_PER_M", "0.006"))
PRICE_OUT_PER_M = float(os.environ.get("STUDYTOOL_PRICE_OUT_PER_M", "1.20"))
MAX_AI_CONCURRENCY = int(os.environ.get("STUDYTOOL_MAX_AI_CONCURRENCY", "4"))
AI_MAX_RETRIES = 4

# Bump when the logic changes so cached results are not reused.
EXTRACTOR_VERSION = "2"
SEGMENT_PROMPT_VERSION = "4"
GENERATE_PROMPT_VERSION = "3"

WORKER_HEARTBEAT_SECONDS = 5
WORKER_STALE_SECONDS = 30  # a worker silent this long is treated as dead

for _d in (DATA_DIR, PDF_DIR, PAGE_IMG_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
