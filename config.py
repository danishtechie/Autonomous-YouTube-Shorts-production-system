"""
config.py — Centralized configuration loader with validation.
All modules import from here. Never read .env directly elsewhere.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

# Load .env from project root
load_dotenv(Path(__file__).parent / ".env")


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        logger.error(f"Missing required env var: {key}")
        sys.exit(1)
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ── LLM ──────────────────────────────────────────────────────────────────────
LLM_PROVIDER      = _optional("LLM_PROVIDER", "anthropic")
LLM_MODEL         = _optional("LLM_MODEL", "claude-3-5-haiku-20241022")
ANTHROPIC_API_KEY = _optional("ANTHROPIC_API_KEY")
OPENAI_API_KEY    = _optional("OPENAI_API_KEY")

# ── TTS ───────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY = _optional("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = _optional("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# ── Stock Footage ─────────────────────────────────────────────────────────────
PEXELS_API_KEY   = _optional("PEXELS_API_KEY")
PIXABAY_API_KEY  = _optional("PIXABAY_API_KEY")

# ── YouTube ───────────────────────────────────────────────────────────────────
YOUTUBE_CLIENT_ID      = _optional("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET  = _optional("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN  = _optional("YOUTUBE_REFRESH_TOKEN")
YOUTUBE_CHANNEL_ID     = _optional("YOUTUBE_CHANNEL_ID")

# ── Reddit ────────────────────────────────────────────────────────────────────
REDDIT_CLIENT_ID     = _optional("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = _optional("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT    = _optional("REDDIT_USER_AGENT", "YTShortsBot/1.0")

# ── SerpAPI ───────────────────────────────────────────────────────────────────
SERPAPI_KEY = _optional("SERPAPI_KEY")

# ── System ────────────────────────────────────────────────────────────────────
VIDEOS_PER_DAY      = int(_optional("VIDEOS_PER_DAY", "2"))
UPLOAD_TIMES        = _optional("UPLOAD_TIMES", "09:00,17:00").split(",")
NICHE               = _optional("NICHE", "facts")
MAX_VIDEO_DURATION  = int(_optional("MAX_VIDEO_DURATION", "58"))
MIN_VIDEO_DURATION  = int(_optional("MIN_VIDEO_DURATION", "30"))

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
OUTPUT_DIR  = Path(_optional("OUTPUT_DIR", "./output"))
LOGS_DIR    = Path(_optional("LOGS_DIR", "./logs"))
DB_PATH     = Path(_optional("DB_PATH", "./data/bot.db"))

VIDEO_DIR    = OUTPUT_DIR / "videos"
AUDIO_DIR    = OUTPUT_DIR / "audio"
SUBTITLE_DIR = OUTPUT_DIR / "subtitles"

# Create dirs
for d in [OUTPUT_DIR, LOGS_DIR, VIDEO_DIR, AUDIO_DIR, SUBTITLE_DIR,
          DB_PATH.parent]:
    d.mkdir(parents=True, exist_ok=True)

# ── Retry ─────────────────────────────────────────────────────────────────────
MAX_RETRIES  = int(_optional("MAX_RETRIES", "3"))
RETRY_DELAY  = int(_optional("RETRY_DELAY", "30"))

# ── Logging setup ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> — {message}",
    level="INFO",
)
logger.add(
    LOGS_DIR / "bot_{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention="30 days",
    level="DEBUG",
    encoding="utf-8",
)
