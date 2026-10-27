"""
LankaPulse — background pipeline.

ingest -> persist raw queue -> corroborate -> extract -> geocode/dedupe/store
       -> broadcast over WebSocket

The raw queue lives in SQLite, so restarts never re-process old articles and
items that exceed the per-cycle budget are picked up on the next cycle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from sqlalchemy import and_, delete, or_, select, update

from ai_processor import event_to_dict, extract_event_with_llm, process_and_store_event
from config import Settings
from geocoder import sri_lanka_signal_score, text_has_sri_lanka_signal
from grounder import ground_event
from ingestor import RawNewsItem, extract_domain, run_ingestion_cycle
from models import RawItem, utcnow

logger = logging.getLogger("lankapulse.pipeline")

Broadcast = Callable[[dict], Awaitable[None]]


@dataclass
class PipelineState:
    running: bool = False
    last_started: Optional[datetime] = None
    last_finished: Optional[datetime] = None
    last_error: str = ""
    last_summary: dict = field(default_factory=dict)
    total_cycles: int = 0

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "last_started": self.last_started.isoformat() if self.last_started else "",
            "last_finished": self.last_finished.isoformat() if self.last_finished else "",
            "last_error": self.last_error,
            "last_summary": self.last_summary,
            "total_cycles": self.total_cycles,
        }


state = PipelineState()
_cycle_lock = asyncio.Lock()


async def _insert_new_items(session, items: list[RawNewsItem]) -> int:
    if not items:
        return 0

    hashes = list({item.url_hash for item in items})
    known: set[str] = set()
    for start in range(0, len(hashes), 400):
        chunk = hashes[start:start + 400]
        result = await session.execute(
            select(RawItem.url_hash).where(RawItem.url_hash.in_(chunk))
        )
        known.update(result.scalars().all())

    inserted = 0
    for item in items:
        if item.url_hash in known:
            continue
        known.add(item.url_hash)
        session.add(RawItem(
            url_hash=item.url_hash,
            url=item.url[:1024],
            title=item.title[:1000],
            summary=item.summary,
            source_name=item.source_name[:250],
            platform=item.platform,
            language=item.language,
            published_at=item.published_at,
            status="pending",
        ))
        inserted += 1

    await session.commit()
    return inserted


async def _pending_ids(session, limit: int) -> list[int]:
    """Pick the freshest items that look Sri Lanka-related.

    The queue is intentionally larger than the per-cycle budget; relevance and
    recency decide which items get the expensive corroboration + extraction.
    """
    result = await session.execute(
        select(RawItem)
        .where(RawItem.status == "pending")
        .order_by(RawItem.discovered_at.desc())
        .limit(300)
    )
    items = list(result.scalars().all())
    items.sort(
        key=lambda item: (
            sri_lanka_signal_score(f"{item.title} {item.summary or ''}"),
            item.published_at or item.discovered_at,
        ),
        reverse=True,
    )
    return [item.id for item in items[:limit]]


async def _process_item(
    session_factory,
    item_id: int,
    settings: Settings,
    broadcast: Optional[Broadcast],
    summary: dict,
) -> None:
    async with session_factory() as session:
        item = await session.get(RawItem, item_id)
        if item is None or item.status != "pending":
            return

        try:
            if not text_has_sri_lanka_signal(f"{item.title} {item.summary or ''}"):
                reason = "no Sri Lankan location or reference"
                await session.execute(
                    update(RawItem)
                    .where(RawItem.id == item_id)
                    .values(status="skipped", skip_reason=reason, processed_at=utcnow())
                )
                await session.commit()
                summary["skipped"] = summary.get("skipped", 0) + 1
                logger.info("Skipped raw item #%s: %s", item_id, reason)
                return

            bundle = await ground_event(
                title=item.title,
                description=item.summary or "",
                language=item.language or "EN",
                exclude_domain=extract_domain(item.url),
                primary_url=item.url,
                settings=settings,
            )

            outcome = await extract_event_with_llm(
                raw_title=item.title,
                raw_description=item.summary or "",
                grounded=bundle,
                source_name=item.source_name or "Unknown source",
                source_url=item.url,
                language=item.language or "EN",
                platform=item.platform or "RSS",
                provider="rss",
                published_at=item.published_at,
                settings=settings,
            )

            store = await process_and_store_event(session, outcome, bundle, settings)
            now = utcnow()

            if store.event is not None:
                await session.execute(
                    update(RawItem)
                    .where(RawItem.id == item_id)
                    .values(
                        status="processed",
                        event_id=store.event.id,
                        processed_at=now,
                        skip_reason="",
                    )
                )
                await session.commit()

                summary["processed"] = summary.get("processed", 0) + 1
                summary["extractors"] = sorted(
                    set(summary.get("extractors", [])) | {outcome.extractor}
                )
                if broadcast:
                    try:
                        await broadcast({
                            "type": "event_update",
                            "event": event_to_dict(store.event),
                        })
                    except Exception as exc:  # broadcast failure must not fail storage
                        logger.warning("WebSocket broadcast failed: %s", exc)
            else:
                reason = (store.skip_reason or "not stored")[:250]
                await session.execute(
                    update(RawItem)
                    .where(RawItem.id == item_id)
                    .values(status="skipped", skip_reason=reason, processed_at=now)
                )
                await session.commit()
                summary["skipped"] = summary.get("skipped", 0) + 1
                logger.info("Skipped raw item #%s: %s", item_id, reason)

        except Exception as exc:
            logger.exception("Pipeline item #%s failed", item_id)
            await session.rollback()
            try:
                item = await session.get(RawItem, item_id)
                if item is not None:
                    item.status = "failed"
                    item.processed_at = utcnow()
                    item.skip_reason = f"{exc.__class__.__name__}: {exc}"[:250]
                    await session.commit()
            except Exception:
                logger.exception("Could not mark raw item #%s as failed", item_id)
            summary["failed"] = summary.get("failed", 0) + 1


async def _prune(session_factory, settings: Settings) -> int:
    if settings.raw_item_retention_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=settings.raw_item_retention_days)
    async with session_factory() as session:
        result = await session.execute(
            delete(RawItem).where(
                or_(
                    and_(RawItem.processed_at.is_not(None), RawItem.processed_at < cutoff),
                    and_(RawItem.status == "pending", RawItem.discovered_at < cutoff),
                )
            )
        )
        await session.commit()
        return result.rowcount or 0


async def run_cycle(
    session_factory,
    settings: Settings,
    broadcast: Optional[Broadcast] = None,
    ingest: bool = True,
) -> dict:
    """Run one pipeline cycle. Safe to call concurrently; duplicates are skipped."""
    if _cycle_lock.locked():
        return {"skipped": "cycle already running"}

    async with _cycle_lock:
        state.running = True
        state.last_started = utcnow()
        summary: dict = {
            "ingested": 0,
            "inserted": 0,
            "processed": 0,
            "skipped": 0,
            "failed": 0,
        }

        try:
            if ingest:
                items = await run_ingestion_cycle(settings.youtube_channel_ids)
                summary["ingested"] = len(items)
                async with session_factory() as session:
                    summary["inserted"] = await _insert_new_items(session, items)

            async with session_factory() as session:
                ids = await _pending_ids(session, settings.max_items_per_cycle)

            summary["queued"] = len(ids)

            for index, item_id in enumerate(ids):
                await _process_item(session_factory, item_id, settings, broadcast, summary)
                if index < len(ids) - 1 and settings.item_pause_seconds:
                    await asyncio.sleep(settings.item_pause_seconds)

            pruned = await _prune(session_factory, settings)
            summary["pruned"] = pruned
            state.last_error = ""
            state.total_cycles += 1
            return summary

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Pipeline cycle failed")
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            summary["error"] = state.last_error
            return summary
        finally:
            state.running = False
            state.last_finished = utcnow()
            state.last_summary = summary
            logger.info(
                "Cycle finished: ingested=%s inserted=%s processed=%s skipped=%s failed=%s",
                summary.get("ingested"), summary.get("inserted"),
                summary.get("processed"), summary.get("skipped"), summary.get("failed"),
            )


async def pipeline_loop(
    session_factory,
    settings: Settings,
    broadcast: Optional[Broadcast] = None,
) -> None:
    logger.info(
        "Pipeline loop started (interval=%ss, batch=%s)",
        settings.poll_interval_seconds, settings.max_items_per_cycle,
    )
    while True:
        try:
            await run_cycle(session_factory, settings, broadcast)
        except asyncio.CancelledError:
            logger.info("Pipeline loop cancelled")
            raise
        except Exception:
            logger.exception("Unexpected pipeline loop error")
        await asyncio.sleep(settings.poll_interval_seconds)
