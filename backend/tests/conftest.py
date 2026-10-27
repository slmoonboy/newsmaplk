"""Shared pytest configuration for the LankaPulse backend tests."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Configure the app *before* any project module is imported: settings are
# cached at import time and the test suite must never touch the network.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="lankapulse-tests-"))

os.environ.update(
    {
        "DATABASE_URL": f"sqlite+aiosqlite:///{(_TMP_DIR / 'api.db').as_posix()}",
        "ENABLE_PIPELINE": "false",
        "ENABLE_GROUNDING": "false",
        "ENABLE_ARTICLE_FETCH": "false",
        "ENABLE_GDELT": "false",
        "LLM_PROVIDER": "heuristic",
        "GROUNDING_CACHE_SECONDS": "0",
        "ADMIN_TOKEN": "",
        "LOG_LEVEL": "WARNING",
    }
)

import pytest  # noqa: E402  (env must be set first)

from config import settings  # noqa: E402
from models import (  # noqa: E402
    Event,
    MediaReport,
    create_db_engine,
    create_session_factory,
    init_db,
    utcnow,
)


@pytest.fixture(scope="session")
def backend_dir() -> Path:
    return BACKEND_DIR


@pytest.fixture(scope="session")
def app_client():
    """A TestClient with the real lifespan, but no background pipeline."""
    from fastapi.testclient import TestClient

    import main as app_main

    with TestClient(app_main.app) as client:
        yield client


@pytest.fixture(scope="session")
def seeded_event_id(app_client) -> int:
    """Insert one deterministic event directly into the API database."""
    import asyncio

    async def _seed() -> int:
        engine = create_db_engine(settings.database_url)
        await init_db(engine)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            event = Event(
                event_hash="test-seed-event",
                title_en="Demo test event: flooding in Ratnapura",
                title_si="",
                title_ta="",
                what_happened="Heavy rain flooded low-lying roads in Ratnapura town.",
                how_it_happened=["Rain began overnight.", "Roads reopened by noon."],
                category="WEATHER",
                confidence_score=0.8,
                extractor="demo",
                town="Ratnapura",
                district="Ratnapura",
                latitude=6.6828,
                longitude=80.3992,
                location_precision="exact",
                is_active=True,
                is_verified=True,
                source_count=2,
                grounding_providers=["demo"],
                first_reported_at=utcnow(),
                last_updated_at=utcnow(),
                created_at=utcnow(),
            )
            session.add(event)
            await session.flush()
            for outlet, url in (
                ("Ada Derana", "https://example.com/demo/ratnapura"),
                ("Daily Mirror", "https://example.com/demo/ratnapura-2"),
            ):
                session.add(
                    MediaReport(
                        event_id=event.id,
                        outlet_name=outlet,
                        platform="RSS",
                        language="EN",
                        headline_or_quote=event.title_en,
                        url=url,
                        published_at=utcnow(),
                        provider="demo",
                    )
                )
            await session.commit()
            event_id = event.id
        await engine.dispose()
        return event_id

    return asyncio.run(_seed())


@pytest.fixture()
def temp_session_factory(tmp_path):
    """A fresh SQLite database + session factory for persistence tests."""
    import asyncio

    url = f"sqlite+aiosqlite:///{(tmp_path / 'unit.db').as_posix()}"
    engine = create_db_engine(url)
    asyncio.run(init_db(engine))
    return engine, create_session_factory(engine)
