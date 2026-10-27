"""
LankaPulse — FastAPI server.

REST API + WebSocket + static frontend, backed by SQLite.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, desc, func, select, text
from sqlalchemy.orm import selectinload

import grounder
import ingestor
from ai_processor import event_to_dict, llm_status
from config import ROOT_DIR, settings
from models import (
    Event,
    MediaReport,
    RawItem,
    create_db_engine,
    create_session_factory,
    init_db,
    utcnow,
)
from pipeline import pipeline_loop, run_cycle, state as pipeline_state

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lankapulse.main")

FRONTEND_DIR = ROOT_DIR / "frontend"
MAX_EVENT_LIMIT = 200
TRIGGER_COOLDOWN_SECONDS = 20


# ─── WebSocket manager ───────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.add(websocket)
        logger.info("WebSocket connected (%d active)", len(self.active))

    def disconnect(self, websocket: WebSocket) -> None:
        self.active.discard(websocket)
        logger.info("WebSocket disconnected (%d active)", len(self.active))

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for connection in list(self.active):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


ws_manager = ConnectionManager()


# ─── App lifecycle ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("LankaPulse starting (pipeline=%s, llm=%s/%s)",
                settings.enable_pipeline, settings.llm_provider, settings.llm_model)

    engine = create_db_engine()
    await init_db(engine)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.started_at = utcnow()

    task: Optional[asyncio.Task] = None
    if settings.enable_pipeline:
        task = asyncio.create_task(
            pipeline_loop(app.state.session_factory, settings, ws_manager.broadcast)
        )

    yield

    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    await grounder.shutdown()
    await ingestor.close_http_client()
    await engine.dispose()
    logger.info("LankaPulse stopped")


app = FastAPI(
    title="LankaPulse",
    description="Real-time, open-source breaking-news map of Sri Lanka",
    version="1.1.0",
    lifespan=lifespan,
)

_origins = list(settings.cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials="*" not in _origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=800)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def _session_factory(request: Request):
    return request.app.state.session_factory


async def _scalar_count(session, conditions) -> int:
    stmt = select(func.count()).select_from(Event)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return int((await session.execute(stmt)).scalar_one())


# ─── Frontend ────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path), media_type="text/html")
    return JSONResponse({"message": "LankaPulse API", "docs": "/docs"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


# ─── Health / metadata ───────────────────────────────────────

@app.get("/api/health", tags=["meta"])
async def health(request: Request):
    session_factory = _session_factory(request)
    db_ok = True
    counts = {"events": 0, "media_reports": 0, "raw_pending": 0}
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
            counts["events"] = int(
                (await session.execute(select(func.count()).select_from(Event))).scalar_one()
            )
            counts["media_reports"] = int(
                (await session.execute(select(func.count()).select_from(MediaReport))).scalar_one()
            )
            counts["raw_pending"] = int(
                (await session.execute(
                    select(func.count()).select_from(RawItem).where(RawItem.status == "pending")
                )).scalar_one()
            )
    except Exception as exc:
        db_ok = False
        logger.warning("Health database check failed: %s", exc)

    started = getattr(request.app.state, "started_at", None)
    return {
        "status": "ok" if db_ok else "degraded",
        "version": app.version,
        "uptime_seconds": int((utcnow() - started).total_seconds()) if started else 0,
        "database": {"ok": db_ok, **counts},
        "pipeline": pipeline_state.as_dict(),
        "llm": llm_status(settings),
        "grounding": {
            "enabled": settings.enable_grounding,
            "providers": ["google_news", "wikipedia"] + (["gdelt"] if settings.enable_gdelt else []),
        },
        "youtube_rss": bool(settings.youtube_channel_ids),
    }


@app.get("/api/sources", tags=["meta"])
async def sources():
    return {
        "feeds": ingestor.source_catalog(),
        "trusted_domains": list(ingestor.TRUSTED_DOMAINS),
        "grounding": {
            "google_news_rss": "https://news.google.com/rss",
            "wikipedia": "https://www.wikipedia.org",
            "gdelt": "https://api.gdeltproject.org" if settings.enable_gdelt else None,
            "nominatim": "https://nominatim.openstreetmap.org",
        },
        "licenses": {
            "OpenStreetMap": "ODbL 1.0",
            "geoBoundaries": "ODbL 1.0",
            "Wikipedia": "CC BY-SA 4.0",
            "MapLibre GL JS": "BSD-3-Clause",
            "OpenFreeMap": "free/open-source map tiles",
        },
        "llm": llm_status(settings),
    }


# ─── Events ──────────────────────────────────────────────────

@app.get("/api/events", tags=["events"])
async def get_events(
    request: Request,
    active_only: bool = Query(True),
    hours: int = Query(24, ge=0, le=720, description="0 = no time limit"),
    district: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=MAX_EVENT_LIMIT),
    offset: int = Query(0, ge=0),
):
    conditions = []
    if active_only:
        conditions.append(Event.is_active.is_(True))
    if hours > 0:
        conditions.append(Event.created_at >= utcnow() - timedelta(hours=hours))
    if district:
        conditions.append(func.lower(Event.district) == district.strip().lower())
    if category:
        conditions.append(func.upper(Event.category) == category.strip().upper())

    async with _session_factory(request)() as session:
        total = await _scalar_count(session, conditions)

        stmt = select(Event)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = (
            stmt.options(selectinload(Event.media_reports))
            .order_by(desc(Event.last_updated_at))
            .limit(limit)
            .offset(offset)
        )
        events = (await session.execute(stmt)).scalars().all()

        return {
            "count": len(events),
            "total": total,
            "limit": limit,
            "offset": offset,
            "events": [event_to_dict(event) for event in events],
        }


@app.get("/api/events/{event_id}", tags=["events"])
async def get_event(request: Request, event_id: int):
    async with _session_factory(request)() as session:
        result = await session.execute(
            select(Event)
            .where(Event.id == event_id)
            .options(selectinload(Event.media_reports))
        )
        event = result.scalar_one_or_none()
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return event_to_dict(event)


@app.get("/api/markers", tags=["events"])
async def get_markers(
    request: Request,
    hours: int = Query(24, ge=0, le=720),
    category: Optional[str] = Query(None),
):
    conditions = [Event.is_active.is_(True)]
    if hours > 0:
        conditions.append(Event.created_at >= utcnow() - timedelta(hours=hours))
    if category:
        conditions.append(func.upper(Event.category) == category.strip().upper())

    async with _session_factory(request)() as session:
        stmt = (
            select(Event)
            .where(and_(*conditions))
            .order_by(desc(Event.last_updated_at))
            .limit(1000)
        )
        events = (await session.execute(stmt)).scalars().all()
        now = utcnow()
        markers = []
        for event in events:
            reference = event.first_reported_at or event.created_at or now
            markers.append({
                "id": event.id,
                "title_en": event.title_en,
                "title_si": event.title_si or "",
                "title_ta": event.title_ta or "",
                "town": event.town,
                "district": event.district,
                "category": event.category,
                "latitude": event.latitude,
                "longitude": event.longitude,
                "location_precision": event.location_precision or "exact",
                "source_count": event.source_count,
                "is_active": bool(event.is_active),
                "is_verified": bool(event.is_verified),
                "confidence_score": event.confidence_score,
                "first_reported_at": reference.isoformat() if reference else "",
                "minutes_ago": max(0, int((now - reference).total_seconds() / 60)),
            })
        return {"count": len(markers), "markers": markers}


@app.get("/api/stats", tags=["events"])
async def get_stats(request: Request):
    cutoff_24h = utcnow() - timedelta(hours=24)
    cutoff_1h = utcnow() - timedelta(hours=1)

    async with _session_factory(request)() as session:
        base_24h = [Event.is_active.is_(True), Event.created_at >= cutoff_24h]
        events_24h = await _scalar_count(session, base_24h)
        events_1h = await _scalar_count(
            session, [Event.is_active.is_(True), Event.created_at >= cutoff_1h]
        )
        verified = await _scalar_count(
            session, base_24h + [Event.is_verified.is_(True)]
        )
        districts_active = int((await session.execute(
            select(func.count(func.distinct(Event.district))).where(and_(*base_24h))
        )).scalar_one())

        rows = (await session.execute(
            select(Event.category, func.count())
            .where(and_(*base_24h))
            .group_by(Event.category)
        )).all()
        category_breakdown = {category or "OTHER": int(count) for category, count in rows}

        return {
            "events_24h": events_24h,
            "events_1h": events_1h,
            "verified_24h": verified,
            "districts_active": districts_active,
            "category_breakdown": category_breakdown,
            "ws_connections": len(ws_manager.active),
            "pipeline": pipeline_state.as_dict(),
        }


# ─── Manual trigger (admin) ─────────────────────────────────

_last_trigger = 0.0


@app.post("/api/trigger-cycle", status_code=202, tags=["admin"])
async def trigger_cycle(request: Request):
    global _last_trigger

    if settings.admin_token:
        supplied = request.headers.get("x-admin-token", "")
        if supplied != settings.admin_token:
            raise HTTPException(status_code=401, detail="Invalid admin token")

    if pipeline_state.running:
        raise HTTPException(status_code=409, detail="A pipeline cycle is already running")

    now = time.monotonic()
    if now - _last_trigger < TRIGGER_COOLDOWN_SECONDS:
        retry = int(TRIGGER_COOLDOWN_SECONDS - (now - _last_trigger))
        raise HTTPException(status_code=429, detail=f"Try again in {retry}s")

    _last_trigger = now
    asyncio.create_task(
        run_cycle(request.app.state.session_factory, settings, ws_manager.broadcast)
    )
    return {"status": "queued"}


# ─── WebSocket ───────────────────────────────────────────────

@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        async with websocket.app.state.session_factory() as session:
            cutoff = utcnow() - timedelta(hours=24)
            stmt = (
                select(Event)
                .where(and_(Event.is_active.is_(True), Event.created_at >= cutoff))
                .options(selectinload(Event.media_reports))
                .order_by(desc(Event.last_updated_at))
                .limit(100)
            )
            events = (await session.execute(stmt)).scalars().all()
            await websocket.send_json({
                "type": "initial_state",
                "events": [event_to_dict(event) for event in events],
            })

        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if message == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({
                        "type": "heartbeat",
                        "ts": utcnow().isoformat(),
                    })
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)
    finally:
        ws_manager.disconnect(websocket)


# ─── Entrypoint ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
