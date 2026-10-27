"""
Seed the database with clearly-labelled demo events so the UI can be explored
without waiting for the ingestion pipeline or running a local LLM.

Usage (from the backend directory):

    python seed_demo.py
    python seed_demo.py --clear     # remove only demo rows

All titles start with "Demo:" and every URL uses example.com.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from datetime import timedelta

from sqlalchemy import select

from geocoder import geocode_local
from models import (
    Event,
    MediaReport,
    create_db_engine,
    create_session_factory,
    init_db,
    utcnow,
)

DEMO_EVENTS = [
    {
        "title_en": "Demo: Two injured in collision on Kandy Road",
        "title_si": "Demo: කඩවත අසල මහනුවර මාර්ගයේ අනතුරකින් දෙදෙනෙකු තුවාල ලබයි",
        "title_ta": "Demo: கடவத்தை அருகே கண்டி வீதியில் விபத்தில் இருவர் காயம்",
        "summary": (
            "A van and a three-wheeler collided near Kadawatha on the Kandy Road. "
            "Two people were taken to hospital with injuries that are not life-threatening."
        ),
        "steps": [
            "The collision occurred near the Kadawatha interchange around 08:10.",
            "Emergency crews cleared one lane while traffic was diverted.",
            "Police said both injured people are receiving treatment in hospital.",
        ],
        "category": "ACCIDENT",
        "town": "Kadawatha",
        "district": "Gampaha",
        "hours_ago": 0.4,
        "confidence": 0.78,
        "sources": [
            ("Ada Derana", "https://example.com/demo/kadawatha-collision", "EN", "RSS"),
            ("Daily Mirror", "https://example.com/demo/kadawatha-collision-dm", "EN", "WEB"),
        ],
    },
    {
        "title_en": "Demo: Heavy rain triggers flooding in Ratnapura town",
        "title_si": "Demo: රත්නපුර නගරයේ ගංවතුර",
        "title_ta": "Demo: இரத்தினபுரியில் வெள்ளம்",
        "summary": (
            "Overnight heavy rain caused flash flooding in low-lying parts of Ratnapura town. "
            "The Disaster Management Centre said no evacuations were required."
        ),
        "steps": [
            "Heavy rain began late on Monday evening.",
            "Water entered shops near the bus stand before dawn.",
            "Roads reopened after drains were cleared on Tuesday morning.",
        ],
        "category": "WEATHER",
        "town": "Ratnapura",
        "district": "Ratnapura",
        "hours_ago": 1.2,
        "confidence": 0.84,
        "sources": [
            ("Ada Derana Sinhala", "https://example.com/demo/ratnapura-flood", "SI", "RSS"),
            ("NewsFirst", "https://example.com/demo/ratnapura-flood-nf", "EN", "WEB"),
            ("EconomyNext", "https://example.com/demo/ratnapura-flood-en", "EN", "WEB"),
        ],
    },
    {
        "title_en": "Demo: Police arrest three over smuggling attempt in Mannar",
        "summary": (
            "Police intercepted a boat carrying unregistered goods near Mannar and arrested three people. "
            "The suspects are expected to appear before the magistrate."
        ),
        "steps": [
            "A patrol spotted the boat before dawn.",
            "Officers seized the cargo and detained three suspects.",
            "The case was handed to the local police station for further investigation.",
        ],
        "category": "CRIME",
        "town": "Mannar",
        "district": "Mannar",
        "hours_ago": 2.5,
        "confidence": 0.66,
        "sources": [
            ("Daily Mirror", "https://example.com/demo/mannar-smuggling", "EN", "RSS"),
        ],
    },
    {
        "title_en": "Demo: Power outage affects parts of Jaffna",
        "summary": (
            "A transmission fault left several neighbourhoods in Jaffna without electricity for two hours. "
            "The Ceylon Electricity Board restored supply by midday."
        ),
        "steps": [
            "Supply was interrupted at around 10:05.",
            "Engineers traced the fault to a substation feeder.",
            "Power was restored to all affected areas by 12:20.",
        ],
        "category": "INFRASTRUCTURE",
        "town": "Jaffna",
        "district": "Jaffna",
        "hours_ago": 4.0,
        "confidence": 0.61,
        "sources": [
            ("NewsFirst", "https://example.com/demo/jaffna-outage", "EN", "WEB"),
        ],
    },
    {
        "title_en": "Demo: Protest march blocks Galle Road in Colombo",
        "title_si": "Demo: කොළඹ ගාලු පාරේ විරෝධතා පෙළපාලිය",
        "summary": (
            "A protest march blocked a section of Galle Road in Colombo for about an hour. "
            "Police redirected traffic while organisers handed over a petition."
        ),
        "steps": [
            "Marchers gathered near Kollupitiya at 15:00.",
            "Traffic was diverted along Marine Drive.",
            "The group dispersed after handing a petition to officials.",
        ],
        "category": "POLITICS",
        "town": "Colombo",
        "district": "Colombo",
        "hours_ago": 6.0,
        "confidence": 0.72,
        "sources": [
            ("Ada Derana", "https://example.com/demo/colombo-protest", "EN", "RSS"),
            ("Daily Mirror", "https://example.com/demo/colombo-protest-dm", "EN", "WEB"),
            ("The Island", "https://example.com/demo/colombo-protest-island", "EN", "WEB"),
        ],
    },
    {
        "title_en": "Demo: Vegetable prices rise in Nuwara Eliya markets",
        "summary": (
            "Vegetable prices in Nuwara Eliya increased after several days of bad weather disrupted supplies. "
            "Traders expect prices to ease once transport resumes normally."
        ),
        "steps": [
            "Supplies were delayed by poor road conditions.",
            "Retail prices rose for beans, carrots and leeks.",
            "Traders expect a return to normal within a week.",
        ],
        "category": "ECONOMY",
        "town": "Nuwara Eliya",
        "district": "Nuwara Eliya",
        "hours_ago": 9.0,
        "confidence": 0.58,
        "sources": [
            ("EconomyNext", "https://example.com/demo/nuwara-eliya-prices", "EN", "WEB"),
        ],
    },
    {
        "title_en": "Demo: Landslide closes access road near Badulla",
        "summary": (
            "A minor landslide blocked a rural access road near Badulla after sustained rain. "
            "No injuries were reported and crews were clearing the debris."
        ),
        "steps": [
            "Debris came down onto the road early on Monday.",
            "The road was closed to traffic as a precaution.",
            "Council crews began clearing the route.",
        ],
        "category": "ACCIDENT",
        "town": "Badulla",
        "district": "Badulla",
        "hours_ago": 13.0,
        "confidence": 0.55,
        "location_precision": "approximate",
        "sources": [
            ("Ada Derana Sinhala", "https://example.com/demo/badulla-landslide", "SI", "RSS"),
            ("NewsFirst", "https://example.com/demo/badulla-landslide-nf", "EN", "WEB"),
        ],
    },
    {
        "title_en": "Demo: Community clean-up drive held in Trincomalee",
        "summary": (
            "Volunteers took part in a community clean-up along the Trincomalee waterfront. "
            "Organisers said the collected waste would be recycled."
        ),
        "steps": [
            "Volunteers met at the waterfront at 07:00.",
            "Several bags of waste were collected and sorted.",
            "Organisers plan to repeat the drive next month.",
        ],
        "category": "OTHER",
        "town": "Trincomalee",
        "district": "Trincomalee",
        "hours_ago": 20.0,
        "confidence": 0.45,
        "sources": [
            ("The Island", "https://example.com/demo/trinco-cleanup", "EN", "WEB"),
        ],
    },
]


def _demo_hash(title: str, district: str) -> str:
    return hashlib.sha256(f"demo|{title}|{district}".encode()).hexdigest()[:24]


async def _clear(session) -> int:
    result = await session.execute(select(Event).where(Event.extractor == "demo"))
    events = result.scalars().all()
    for event in events:
        await session.delete(event)
    await session.commit()
    return len(events)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo data for LankaPulse")
    parser.add_argument("--clear", action="store_true", help="remove demo events and exit")
    args = parser.parse_args()

    engine = create_db_engine()
    await init_db(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        if args.clear:
            removed = await _clear(session)
            print(f"Removed {removed} demo event(s).")
            await engine.dispose()
            return

        now = utcnow()
        created = 0
        for spec in DEMO_EVENTS:
            event_hash = _demo_hash(spec["title_en"], spec["district"])
            existing = await session.execute(
                select(Event).where(Event.event_hash == event_hash)
            )
            if existing.scalar_one_or_none() is not None:
                continue

            geo = geocode_local(spec["town"], spec["district"])
            if geo is None:
                print(f"! Could not geocode {spec['town']}, skipping")
                continue

            first_seen = now - timedelta(hours=spec["hours_ago"])
            event = Event(
                event_hash=event_hash,
                title_en=spec["title_en"],
                title_si=spec.get("title_si", ""),
                title_ta=spec.get("title_ta", ""),
                what_happened=spec["summary"],
                how_it_happened=spec["steps"],
                category=spec["category"],
                confidence_score=spec["confidence"],
                extractor="demo",
                town=spec["town"],
                district=spec["district"],
                latitude=geo.latitude,
                longitude=geo.longitude,
                location_precision=spec.get("location_precision", "exact"),
                is_verified=len(spec["sources"]) >= 2,
                source_count=len(spec["sources"]),
                grounding_providers=["demo"],
                first_reported_at=first_seen,
                last_updated_at=first_seen + timedelta(minutes=20),
                created_at=first_seen,
            )
            session.add(event)
            await session.flush()

            for outlet, url, language, platform in spec["sources"]:
                session.add(MediaReport(
                    event_id=event.id,
                    outlet_name=outlet,
                    platform=platform,
                    language=language,
                    headline_or_quote=spec["title_en"],
                    url=url,
                    published_at=first_seen,
                    provider="demo",
                ))
            created += 1

        await session.commit()
        print(f"Seeded {created} demo event(s).")
        print("Start the server with:  uvicorn main:app --reload --port 8000")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
