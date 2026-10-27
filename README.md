# LankaPulse 🇱🇰

**A real-time, location-aware news map of Sri Lanka built entirely from free and open-source tools.**

LankaPulse watches public news feeds, works out *what happened* and *where*, and plots each
incident on a MapLibre map with a live feed, filters, and a source-by-source evidence drawer.
It runs end-to-end with **no API keys, paid services, cloud accounts, or scraping of
login-walled content**.

```
public RSS / Google News RSS ──┐
Wikipedia (background context) ─┤
OpenStreetMap / Nominatim ─────┼──▶ ingestion ──▶ geocoding ──▶ extraction ──▶ dedupe
geoBoundaries district shapes ─┘        │             │             │            │
                                        ▼             ▼             ▼            ▼
                                   SQLite queue   gazetteer    local LLM /    event +
                                   (restart-safe)  + OSM       rules         sources
                                                                   │
                                                        FastAPI REST + WebSocket
                                                                   │
                                                     MapLibre GL JS live map UI
```

---

## Quick start (2 minutes, zero API keys)

Requirements: **Python 3.10+** and a browser. Internet is only needed for live feeds and map tiles.

```bash
git clone https://github.com/slmoonboy/newsmaplk.git
cd newsmaplk

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Optional: 8 clearly-labelled demo events so the map is populated immediately
cd backend
python seed_demo.py
uvicorn main:app --reload --port 8000
```

Open **http://127.0.0.1:8000**.

* The feed, markers and drawer work as soon as the page loads.
* `ENABLE_PIPELINE=true` (default) starts polling public feeds every 3 minutes.
* Without a local LLM the built-in **rule extractor** handles everything, so the app is
  fully functional out of the box.
* Interactive API docs: **http://127.0.0.1:8000/docs**.

### Demo mode only (no network ingestion)

```bash
cd backend
ENABLE_PIPELINE=false uvicorn main:app --port 8000
```

### Remove demo rows

```bash
cd backend
python seed_demo.py --clear
```

---

## Optional: a fully local language model

You do **not** need an LLM. If you want richer extraction, run open-weight
[`qwen2.5:7b-instruct`](https://ollama.com/library/qwen2.5) (Apache-2.0) through
[Ollama](https://ollama.com):

```bash
ollama pull qwen2.5:7b-instruct
ollama serve
```

LankaPulse talks to Ollama through its OpenAI-compatible endpoint
(`http://127.0.0.1:11434/v1`) and automatically falls back to the rule extractor if the
model is missing or slow. Any other OpenAI-compatible server (llama.cpp, vLLM, LM Studio)
works too — set `LLM_PROVIDER=openai_compatible` and `LLM_BASE_URL`.

---

## How it works

| Stage | What happens | Free/open component |
| --- | --- | --- |
| **Collect** | Publisher RSS + Google News RSS queries, filtered to a trusted-domain allowlist | `feedparser`, `httpx` |
| **Queue** | Every URL is hashed and stored in SQLite before processing, so restarts never lose or re-do work | SQLite + SQLAlchemy async |
| **Localise** | Sri Lankan gazetteer (towns, roads, districts) → Nominatim → district centroid fallback | bundled gazetteer, OSM Nominatim |
| **Ground** | Wikipedia summaries as *background context* (never counted as corroboration) | Wikipedia REST API |
| **Extract** | Local LLM (JSON schema) or deterministic rules → category, place, summary, steps, confidence | OpenAI-compatible client + heuristics |
| **Corroborate** | Title-overlap dedupe within 12 h / same district; independent outlets merge into one event | pure Python |
| **Serve** | REST endpoints + `/ws/live` WebSocket push | FastAPI, uvicorn |
| **Display** | Vector map, live feed, category chips, time window, evidence drawer, Sinhala/Tamil titles | MapLibre GL JS, OpenFreeMap |

### Accuracy safeguards

* Events must pass a Sri Lanka signal check and a confidence threshold (`MIN_CONFIDENCE`).
* Non-physical items (policy commentary, sports results, market reports) are filtered out.
* Google News results are only accepted from an explicit allowlist of publishers.
* `source_count` counts **independent news outlets**, excluding Wikipedia and press releases.
* Coordinates carry a precision badge (`exact`, `approximate`, `district`).
* Everything is additive: the drawer always shows the original headlines and links.

---

## Configuration

Copy `.env.example` to `.env` and edit. Every default is free.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENABLE_PIPELINE` | `true` | Background ingestion loop |
| `POLL_INTERVAL_SECONDS` | `180` | Seconds between cycles (minimum 30) |
| `MAX_ITEMS_PER_CYCLE` | `6` | Items processed per cycle (rate-limit friendly) |
| `LLM_PROVIDER` | `auto` | `auto` / `ollama` / `openai_compatible` / `heuristic` |
| `LLM_BASE_URL` | `http://127.0.0.1:11434/v1` | Local OpenAI-compatible endpoint |
| `LLM_MODEL` | `qwen2.5:7b-instruct` | Open-weights model |
| `MIN_CONFIDENCE` | `0.35` | Reject low-confidence extractions |
| `ENABLE_GROUNDING` | `true` | Enable Google News RSS + Wikipedia corroboration |
| `ENABLE_GDELT` | `false` | Optional open-data provider (heavily rate-limited) |
| `ENABLE_ARTICLE_FETCH` | `true` | Fetch the publisher page for extra text |
| `DATABASE_URL` | `sqlite+aiosqlite:///<project>/data/lankapulse.db` | Storage |
| `ADMIN_TOKEN` | *empty* | If set, required by `POST /api/trigger-cycle` |

See [`docs/OPEN_SOURCE_STACK.md`](docs/OPEN_SOURCE_STACK.md) for licences, endpoints,
rate limits, attribution and known limitations.

---

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Serves the map UI |
| `GET` | `/api/health` | DB, pipeline and LLM status |
| `GET` | `/api/events` | `hours`, `district`, `category`, `limit`, `offset`, `active_only` |
| `GET` | `/api/events/{id}` | One event with every media report |
| `GET` | `/api/markers` | Lightweight map markers (`hours`, `category`) |
| `GET` | `/api/stats` | 24 h totals, category and district breakdowns |
| `GET` | `/api/sources` | Feed catalogue, trusted domains, licences |
| `POST` | `/api/trigger-cycle` | Run one ingestion cycle now (20 s cooldown) |
| `WS` | `/ws/live` | Initial state + `event_update`, `heartbeat`, `pong` |

Example:

```bash
curl "http://127.0.0.1:8000/api/events?hours=24&category=WEATHER&limit=5"
curl -X POST http://127.0.0.1:8000/api/trigger-cycle -H "x-admin-token: $ADMIN_TOKEN"
```

---

## Tests

```bash
pip install -r requirements-dev.txt
cd backend
python -m pytest -q
```

The suite (25 tests) runs fully offline: environment variables in
`backend/tests/conftest.py` disable the pipeline, grounding, article fetching and the LLM.

Lint:

```bash
ruff check backend scripts --select F,E9
```

---

## Docker

```bash
docker compose up --build
# → http://127.0.0.1:8000
```

The image runs the real ingestion pipeline, stores SQLite on the `lankapulse-data`
volume, and exposes a health check. To use a host Ollama instance, keep
`LLM_BASE_URL=http://host.docker.internal:11434/v1` (already set in
`docker-compose.yml`). For a demo-only container:

```bash
ENABLE_PIPELINE=false docker compose up --build
```

---

## Refresh the district boundaries

The 25 district outlines are committed, so this is only needed when you want to update
them. The script uses the Python standard library only:

```bash
python scripts/fetch_boundaries.py          # rewrite frontend/sri-lanka-districts.geojson
python scripts/fetch_boundaries.py --check  # validate without writing
```

---

## Project layout

```
backend/            FastAPI app, ingestion, geocoding, extraction, tests
  tests/            25 offline tests (unit + API + WebSocket)
frontend/           MapLibre UI (no build step) + committed district GeoJSON
scripts/            Boundary refresh helper
docs/               Open-source stack, licences and limitations
data/               SQLite database (git-ignored, created at runtime)
```

---

## Responsible use & limitations

* **Public feeds only.** LankaPulse reads publisher RSS and public search feeds. It does not
  bypass paywalls, logins, robots restrictions, or rate limits. Respect each outlet's terms.
* **Not an emergency service.** Extracted summaries can be wrong or stale. Always follow the
  original source linked in the drawer and official announcements.
* **LLMs can hallucinate.** Rule-based extraction is deliberately conservative, and low
  confidence events are dropped rather than guessed.
* **Free endpoints are best-effort.** Google News and Nominatim may throttle; the pipeline
  backs off, and the app keeps serving the last known events.
* **Localisation is approximate.** A district-centroid fallback is labelled as such.

---

## Licence

Code: **MIT** — see [`LICENSE`](LICENSE).

Data and third-party components keep their own licences:

* District boundaries © [geoBoundaries](https://www.geoboundaries.org/) — **ODbL 1.0**
* Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) — **ODbL 1.0**
* Tiles by [OpenFreeMap](https://openfreemap.org/) (free, no key)
* [MapLibre GL JS](https://maplibre.org/) — **BSD-3-Clause**
* Wikipedia text — **CC BY-SA 4.0**
* News headlines remain © their publishers; links and attribution are always shown.

Attribution is rendered on the map and must stay visible when redistributing.
