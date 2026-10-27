"""
LankaPulse — configuration.

Every setting has a free / open-source friendly default:
no API keys are required to run the application.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

# Project-root .env wins, then backend/.env for local overrides.
load_dotenv(ROOT_DIR / ".env")
load_dotenv(BASE_DIR / ".env", override=True)


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _as_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _as_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    # Server
    host: str
    port: int
    log_level: str
    cors_origins: tuple[str, ...]

    # Database
    database_url: str

    # Pipeline
    enable_pipeline: bool
    poll_interval_seconds: int
    max_items_per_cycle: int
    item_pause_seconds: float
    raw_item_retention_days: int

    # LLM (local, open weights by default)
    llm_provider: str  # auto | ollama | openai_compatible | heuristic
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout_seconds: float
    llm_max_tokens: int
    min_confidence: float
    heuristic_fallback: bool

    # Grounding / corroboration
    enable_grounding: bool
    grounding_max_sources: int
    grounding_timeout_seconds: float
    grounding_cache_seconds: int
    enable_article_fetch: bool
    enable_gdelt: bool

    # Optional YouTube ingestion through public channel RSS feeds
    youtube_channel_ids: tuple[str, ...]

    # Admin
    admin_token: str

    @classmethod
    def from_env(cls) -> "Settings":
        default_db = f"sqlite+aiosqlite:///{(ROOT_DIR / 'data' / 'lankapulse.db').as_posix()}"
        return cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=_as_int("PORT", 8000),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            cors_origins=_as_list("CORS_ORIGINS", ("*",)),
            database_url=os.getenv("DATABASE_URL", "").strip() or default_db,
            enable_pipeline=_as_bool("ENABLE_PIPELINE", True),
            poll_interval_seconds=max(30, _as_int("POLL_INTERVAL_SECONDS", 180)),
            max_items_per_cycle=max(1, _as_int("MAX_ITEMS_PER_CYCLE", 6)),
            item_pause_seconds=max(0.0, _as_float("ITEM_PAUSE_SECONDS", 1.5)),
            raw_item_retention_days=max(1, _as_int("RAW_ITEM_RETENTION_DAYS", 7)),
            llm_provider=os.getenv("LLM_PROVIDER", "auto").strip().lower(),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", "qwen2.5:7b-instruct").strip(),
            llm_api_key=os.getenv("LLM_API_KEY", "ollama").strip(),
            llm_timeout_seconds=max(5.0, _as_float("LLM_TIMEOUT_SECONDS", 90.0)),
            llm_max_tokens=max(256, _as_int("LLM_MAX_TOKENS", 1400)),
            min_confidence=min(1.0, max(0.0, _as_float("MIN_CONFIDENCE", 0.35))),
            heuristic_fallback=_as_bool("HEURISTIC_FALLBACK", True),
            enable_grounding=_as_bool("ENABLE_GROUNDING", True),
            grounding_max_sources=max(1, _as_int("GROUNDING_MAX_SOURCES", 5)),
            grounding_timeout_seconds=max(3.0, _as_float("GROUNDING_TIMEOUT_SECONDS", 12.0)),
            grounding_cache_seconds=max(0, _as_int("GROUNDING_CACHE_SECONDS", 600)),
            enable_article_fetch=_as_bool("ENABLE_ARTICLE_FETCH", True),
            enable_gdelt=_as_bool("ENABLE_GDELT", False),
            youtube_channel_ids=_as_list("YOUTUBE_CHANNEL_IDS"),
            admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def reset_settings() -> None:
    """Clear the cached settings (used by tests and reloads)."""
    get_settings.cache_clear()


settings = get_settings()
