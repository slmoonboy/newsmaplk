"""
LankaPulse — database models, API schemas and LLM output schemas.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from config import BASE_DIR, get_settings


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite stores datetimes without timezone)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# ─── Event ────────────────────────────────────────────────────

class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_hash = Column(String(64), unique=True, nullable=False, index=True)

    title_en = Column(String(512), nullable=False)
    title_si = Column(String(512), default="")
    title_ta = Column(String(512), default="")
    what_happened = Column(Text, nullable=False)
    how_it_happened = Column(JSON, default=list)
    category = Column(String(64), default="OTHER", index=True)
    confidence_score = Column(Float, default=0.0)
    extractor = Column(String(128), default="")  # model name or "heuristic"

    town = Column(String(256), nullable=False)
    district = Column(String(128), nullable=False, index=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    location_precision = Column(String(24), default="exact")  # exact|approximate|district|country

    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    source_count = Column(Integer, default=1)
    grounding_providers = Column(JSON, default=list)

    first_reported_at = Column(DateTime, default=utcnow)
    last_updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    created_at = Column(DateTime, default=utcnow)

    media_reports = relationship(
        "MediaReport",
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_events_district_active", "district", "is_active"),
        Index("ix_events_created", "created_at"),
        Index("ix_events_active_updated", "is_active", "last_updated_at"),
    )


# ─── Individual source report ─────────────────────────────────

class MediaReport(Base):
    __tablename__ = "media_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)

    outlet_name = Column(String(256), nullable=False)
    platform = Column(String(32), nullable=False)  # WEB | RSS | X_POST | YOUTUBE
    language = Column(String(8), default="EN")     # EN | SI | TA
    headline_or_quote = Column(Text, nullable=False)
    url = Column(String(1024), default="")
    published_at = Column(DateTime, nullable=True)
    thumbnail_url = Column(String(1024), default="")
    author_handle = Column(String(256), default="")
    provider = Column(String(64), default="rss")   # rss|google_news|wikipedia|gdelt|llm|demo

    created_at = Column(DateTime, default=utcnow)

    event = relationship("Event", back_populates="media_reports")

    __table_args__ = (
        Index("ix_media_url", "url"),
        Index("ix_media_event", "event_id"),
    )


# ─── Raw ingestion queue (persistent deduplication) ───────────

class RawItem(Base):
    __tablename__ = "raw_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    url_hash = Column(String(64), unique=True, nullable=False, index=True)
    url = Column(String(1024), default="")
    title = Column(String(1024), nullable=False)
    summary = Column(Text, default="")
    source_name = Column(String(256), default="")
    platform = Column(String(32), default="RSS")
    language = Column(String(8), default="EN")
    published_at = Column(DateTime, nullable=True)

    discovered_at = Column(DateTime, default=utcnow, index=True)
    processed_at = Column(DateTime, nullable=True)
    status = Column(String(16), default="pending", index=True)  # pending|processed|skipped|failed
    skip_reason = Column(String(256), default="")
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        Index("ix_raw_status_discovered", "status", "discovered_at"),
    )


# ─── Pydantic API schemas (documentation / typing) ────────────

class MediaReportSchema(BaseModel):
    id: int
    outlet_name: str
    platform: str
    language: str
    headline_or_quote: str
    url: str
    published_at: Optional[str] = None
    thumbnail_url: str = ""
    author_handle: str = ""
    provider: str = ""

    model_config = {"from_attributes": True}


class EventSchema(BaseModel):
    id: int
    event_hash: str
    title_en: str
    title_si: str
    title_ta: str
    what_happened: str
    how_it_happened: list[str]
    category: str
    confidence_score: float
    extractor: str
    town: str
    district: str
    latitude: float
    longitude: float
    location_precision: str
    is_active: bool
    is_verified: bool
    source_count: int
    grounding_providers: list[str]
    first_reported_at: str
    last_updated_at: str
    media_reports: list[MediaReportSchema] = Field(default_factory=list)

    model_config = {"from_attributes": True}


# ─── LLM structured-output schema ─────────────────────────────

class LLMMediaReport(BaseModel):
    outlet_name: str = Field(description="Name of the news outlet")
    platform: str = Field("WEB", description="WEB | RSS | X_POST | YOUTUBE")
    language: str = Field("EN", description="EN | SI | TA")
    reported_headline_or_quote: str = Field(description="Exact headline or quote")
    url: str = Field("", description="Direct URL, or empty if unknown")
    published_at: Optional[str] = Field(None, description="ISO-8601 timestamp")
    provider: str = Field("", description="Internal provenance tag")


class LLMExtractionResult(BaseModel):
    is_physical_sri_lanka_event: bool = Field(
        description="Is this a real, physical incident in Sri Lanka?"
    )
    event_title_en: str = Field(description="Concise English headline")
    event_title_si: str = Field("", description="Sinhala headline, empty if unknown")
    event_title_ta: str = Field("", description="Tamil headline, empty if unknown")
    what_happened_summary: str = Field(description="Objective 2-3 sentence overview")
    how_it_happened_steps: list[str] = Field(default_factory=list)
    category: str = Field(
        description="ACCIDENT | WEATHER | POLITICS | CRIME | ECONOMY | INFRASTRUCTURE | OTHER"
    )
    extracted_town_or_junction: str = Field(description="Actual incident site")
    district: str = Field(description="One of the 25 districts of Sri Lanka")
    confidence_score: float = Field(0.5, description="0.0 to 1.0")
    media_reports: list[LLMMediaReport] = Field(default_factory=list)


# ─── Engine / session helpers ─────────────────────────────────

def get_database_url() -> str:
    return get_settings().database_url


def _ensure_sqlite_dir(url: str) -> None:
    if not url.startswith("sqlite"):
        return
    path = url.split("///", 1)[-1]
    if path in {"", ":memory:"} or path.startswith(":memory:"):
        return
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(url: Optional[str] = None):
    url = url or get_database_url()
    _ensure_sqlite_dir(url)
    kwargs: dict = {"echo": False, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    return create_async_engine(url, **kwargs)


def create_session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db(engine) -> None:
    """Create tables and enable SQLite WAL for concurrent reads/writes."""
    async with engine.begin() as conn:
        if engine.url.get_backend_name() == "sqlite":
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.exec_driver_sql("PRAGMA busy_timeout=30000")
        await conn.run_sync(Base.metadata.create_all)
