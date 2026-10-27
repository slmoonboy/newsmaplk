"""Extraction, parsing, deduplication and persistence tests (offline)."""

from __future__ import annotations

import asyncio

import pytest

from ai_processor import (
    extract_event_with_llm,
    parse_json_object,
    process_and_store_event,
    title_similarity,
)
from config import settings
from grounder import GroundedContextBundle
from models import (
    Event,
    RawItem,
    create_db_engine,
    create_session_factory,
    init_db,
    utcnow,
)


def test_parse_json_object_handles_fences_prose_and_trailing_commas():
    payload = '```json\n{"a": 1, "b": [1, 2,],}\n```'
    assert parse_json_object(payload) == {"a": 1, "b": [1, 2]}

    assert parse_json_object('Sure, here you go: {"x": "y"} — done.') == {"x": "y"}


def test_parse_json_object_rejects_non_json():
    with pytest.raises(ValueError):
        parse_json_object("no json here")
    with pytest.raises(ValueError):
        parse_json_object("")


def test_title_similarity_scores_rewrites_and_unrelated_headlines():
    high = title_similarity(
        "Two injured in Kadawatha collision on Kandy Road",
        "Two injured after collision near Kadawatha",
    )
    low = title_similarity(
        "Two injured in Kadawatha collision",
        "Vegetable prices rise in Nuwara Eliya",
    )
    assert high >= 0.55
    assert low < 0.2


async def _extract(title: str, summary: str, source: str, url: str):
    return await extract_event_with_llm(
        raw_title=title,
        raw_description=summary,
        grounded=GroundedContextBundle(query=title),
        source_name=source,
        source_url=url,
        language="EN",
        platform="RSS",
        provider="rss",
        published_at=utcnow(),
        settings=settings,
    )


def test_heuristic_extraction_without_llm():
    outcome = asyncio.run(
        _extract(
            "Two injured in Kadawatha collision on Kandy Road",
            "A van and a three-wheeler collided in Kadawatha, Gampaha district. "
            "Two people were taken to hospital.",
            "Ada Derana",
            "https://example.com/a",
        )
    )

    assert outcome.extractor == "heuristic"
    result = outcome.result
    assert result.is_physical_sri_lanka_event is True
    assert result.district == "Gampaha"
    assert result.extracted_town_or_junction == "Kadawatha"
    assert result.category == "ACCIDENT"
    assert result.confidence_score >= settings.min_confidence
    assert result.media_reports[0].outlet_name == "Ada Derana"
    assert result.media_reports[0].provider == "rss"


def test_non_physical_item_is_rejected():
    outcome = asyncio.run(
        _extract(
            "Sri Lanka and China discuss cultural cooperation",
            "Officials met in Beijing to discuss a cultural exchange programme.",
            "Ada Derana",
            "https://example.com/culture",
        )
    )
    assert outcome.result.is_physical_sri_lanka_event is False


def test_deduplication_merges_reports(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'dedupe.db').as_posix()}"

    async def scenario() -> tuple[int, int, int, bool]:
        engine = create_db_engine(url)
        await init_db(engine)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                first = await _extract(
                    "Two injured in Kadawatha collision on Kandy Road",
                    "A van and a three-wheeler collided in Kadawatha, Gampaha district.",
                    "Ada Derana",
                    "https://example.com/a",
                )
                stored = await process_and_store_event(
                    session, first, GroundedContextBundle(query="a"), settings
                )
                assert stored.event is not None

                second = await _extract(
                    "Two injured after collision near Kadawatha",
                    "The collision happened in Kadawatha, Gampaha district. Both victims are stable.",
                    "Daily Mirror",
                    "https://example.com/b",
                )
                merged = await process_and_store_event(
                    session, second, GroundedContextBundle(query="b"), settings
                )
                assert merged.event is not None
                event = merged.event
                return (
                    stored.event.id,
                    event.id,
                    event.source_count,
                    event.is_verified,
                )
        finally:
            await engine.dispose()

    first_id, second_id, source_count, verified = asyncio.run(scenario())
    assert first_id == second_id
    assert source_count == 2
    assert verified is True


def test_distinct_events_are_not_merged(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'distinct.db').as_posix()}"

    async def scenario() -> tuple[int, int]:
        engine = create_db_engine(url)
        await init_db(engine)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                first = await _extract(
                    "Two injured in Kadawatha collision",
                    "A van collided in Kadawatha, Gampaha district.",
                    "Ada Derana",
                    "https://example.com/a",
                )
                a = await process_and_store_event(
                    session, first, GroundedContextBundle(query="a"), settings
                )
                second = await _extract(
                    "Heavy rain causes flooding in Ratnapura town",
                    "Overnight rain flooded low-lying areas of Ratnapura.",
                    "NewsFirst",
                    "https://example.com/b",
                )
                b = await process_and_store_event(
                    session, second, GroundedContextBundle(query="b"), settings
                )
                assert a.event is not None and b.event is not None
                return a.event.id, b.event.id
        finally:
            await engine.dispose()

    assert len(set(asyncio.run(scenario()))) == 2


def test_raw_item_hash_is_stable():
    first = RawItem(
        url_hash="hash",
        url="https://example.com/x?utm_source=test",
        title="Title",
    )
    assert first.url_hash == "hash"
    assert Event.__tablename__ == "events"
