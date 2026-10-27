# Open-source stack, data sources and limitations

LankaPulse is designed to run on **free and open-source software, open data and public
endpoints only**. This document records exactly what is used, under which licence, what the
operational limits are, and what the project deliberately does *not* do.

Last reviewed: October 2026.

---

## 1. Design principles

1. **No keys, no accounts, no paid tiers.** A fresh clone works without signing up anywhere.
2. **No scraping behind logins or paywalls.** Only public RSS, public search feeds and
   documented open APIs are read.
3. **Graceful degradation.** Every optional provider can fail; the app keeps serving what it
   already has. The rule extractor replaces the LLM when it is unavailable.
4. **Attribution is visible.** Map and data attribution is rendered in the UI, not hidden.
5. **Provenance over polish.** Every event links back to the original headlines; nothing is
   presented as verified without independent sources.

---

## 2. Software components

| Component | Role | Licence | Notes |
| --- | --- | --- | --- |
| [Python](https://www.python.org/) 3.10+ | Runtime | PSF-2.0 | |
| [FastAPI](https://fastapi.tiangolo.com/) | HTTP API + WebSocket | MIT | |
| [uvicorn](https://www.uvicorn.org/) | ASGI server | BSD-3-Clause | `[standard]` extras for WebSocket support |
| [SQLAlchemy](https://www.sqlalchemy.org/) + [aiosqlite](https://github.com/omnilib/aiosqlite) | Async ORM / SQLite driver | MIT | SQLite is public domain |
| [Pydantic](https://docs.pydantic.dev/) | Validation | MIT | |
| [httpx](https://www.python-httpx.org/) | Async HTTP client | BSD-3-Clause | |
| [feedparser](https://feedparser.readthedocs.io/) | RSS/Atom parsing | BSD-2-Clause | |
| [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/) + [lxml](https://lxml.de/) | Article text extraction | MIT / BSD-3-Clause | |
| [openai-python](https://github.com/openai/openai-python) | Client for local OpenAI-compatible servers | Apache-2.0 | Used against Ollama/llama.cpp/vLLM, not a paid API |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` loading | BSD-3-Clause | |
| [MapLibre GL JS](https://maplibre.org/) | Vector map renderer | BSD-3-Clause | Loaded from a public CDN |
| [OpenFreeMap](https://openfreemap.org/) | Free vector tiles + styles | Free service, open data | No key, no signup |
| [Rare UI](https://rareui.com) | Interaction-pattern reference (counters, bell, chip glide, scroll progress, step player) | MIT + Commons Clause 1.0 + Attribution | Component **source is not vendored**; only patterns are re-implemented in vanilla JS/CSS. Metadata indexed locally. See [`RARE_UI.md`](RARE_UI.md) |
| [Skill Issue](https://skill-issue.in/) MCP | Read-only agent-skill catalogue | Public MCP endpoint | No key or account; configured in `opencode.json` |
| [skills CLI](https://github.com/vercel-labs/skills) | Installs local agent skills | MIT | Build-time tooling only; `.agents/` is git-ignored |
| [pytest](https://pytest.org/) / [ruff](https://docs.astral.sh/ruff/) | Tests / lint (dev only) | MIT | |

Frontend fonts (Inter, JetBrains Mono, Noto Sans Sinhala/Tamil) are **SIL Open Font
License 1.1** and served by Google Fonts.

The UI has **no build step** — plain HTML, CSS and ES2020 JavaScript.

### Agent and design tooling (optional, not shipped)

* **Skill Issue MCP** (`skill-issue.in/api/mcp`) is a remote, read-only skill catalogue
  configured project-locally in `opencode.json`. It has no bearing on the running app.
* **Agent skills** are installed into `.agents/skills/` with the MIT-licensed `skills` CLI
  and recorded in `skills-lock.json`. They are local build-time guidance and git-ignored.
* **Rare UI registry index** — `scripts/rare_registry.py` downloads the public registry and
  stores metadata (no component source) in `data/rare-ui.db` using only the Python standard
  library. See [`RARE_UI.md`](RARE_UI.md) for the licence and attribution rules.
* The five Rare UI-inspired interactions are original vanilla implementations and are not
  redistributed Rare UI source. The visible credit is required by the upstream licence.

### Optional language models (open weights)

| Model | Licence | Size | Notes |
| --- | --- | --- | --- |
| `qwen2.5:7b-instruct` (default) | Apache-2.0 | ~4.7 GB | Strong multilingual JSON adherence; good Sinhala/Tamil tolerance |
| Any other Ollama / llama.cpp model | model-specific | — | Set `LLM_MODEL` and `LLM_BASE_URL` |

Inference runs locally through [Ollama](https://ollama.com/) (MIT) or any OpenAI-compatible
server. No prompt or article text leaves the machine.

---

## 3. Data sources

### 3.1 Publisher RSS (direct)

| Publisher | Feed | Language | Notes |
| --- | --- | --- | --- |
| Ada Derana | `adaderana.lk/rss.php` | EN | Public RSS |
| Ada Derana Sinhala | `sinhala.adaderana.lk/rss.php` | SI | Public RSS |
| EconomyNext | `economynext.com/feed/` | EN | WordPress feed |
| The Island | `island.lk/feed/` | EN | WordPress feed |
| BBC Sinhala | `feeds.bbci.co.uk/sinhala/rss.xml` | SI | BBC public feed |
| BBC Tamil | `feeds.bbci.co.uk/tamil/rss.xml` | TA | BBC public feed |

RSS feeds are provided by publishers for syndication. LankaPulse stores only headline,
link, timestamp and a short extracted snippet, and always links to the original article.

### 3.2 Google News RSS

* Endpoint: `https://news.google.com/rss/search?q=…`
* Used for Sinhala/Tamil discovery and for outlets without a usable direct feed
  (Daily Mirror, NewsFirst, Daily FT).
* **Filtering:** results are discarded unless the publisher domain is in
  `TRUSTED_DOMAINS` (see `backend/ingestor.py`), which removes SEO spam, live-stream
  aggregators and unrelated international content.
* Google News article URLs are JavaScript redirects, so article-body extraction is skipped
  for them; the headline and the original publisher link are still stored.
* This is a public, undocumented endpoint. It may change or rate-limit at any time; failures
  are non-fatal. For production use, prefer direct publisher feeds or a licensed news API.

### 3.3 Wikipedia

* Endpoints: `https://{lang}.wikipedia.org/w/api.php` (search + extract), `en`, `si`, `ta`.
* Licence: **CC BY-SA 4.0**; attribution and a link are stored with each snippet.
* Used only as **background context**. It is explicitly **not** counted toward
  `source_count` or the `is_verified` decision.

### 3.4 OpenStreetMap Nominatim

* Endpoint: `https://nominatim.openstreetmap.org/search`
* Licence: **ODbL 1.0** — © OpenStreetMap contributors.
* Policy compliance:
  * maximum **one request per second** (enforced with an async lock + 1.1 s spacing);
  * descriptive `User-Agent` with a project reference;
  * results cached on disk in `backend/.cache/nominatim.json`;
  * only used for towns that are missing from the bundled gazetteer.
* Nominatim is a volunteer service. Heavy use is not acceptable; the intended deployment is
  low-volume. For scale, self-host Nominatim or use another OSM geocoder.

### 3.5 geoBoundaries (district boundaries)

* API: `https://www.geoboundaries.org/api/current/gbOpen/LKA/ADM2/`
* Dataset: LKA ADM2, simplified geometry (25 districts).
* Licence: **ODbL 1.0**; source data from OpenStreetMap / Wambacher.
* Stored as `frontend/sri-lanka-districts.geojson`; refresh with
  `python scripts/fetch_boundaries.py`.

### 3.6 GDELT DOC 2.0 (optional, off by default)

* Endpoint: `https://api.gdeltproject.org/api/v2/doc/doc`
* Open data, but requires short queries and aggressively rate-limits (frequent HTTP 429).
* Enable with `ENABLE_GDELT=true` only if you accept slower cycles and temporary blocks.

### 3.7 YouTube channel RSS (optional, off by default)

* `https://www.youtube.com/feeds/videos.xml?channel_id=…`
* Public feed, no API key. Set `YOUTUBE_CHANNEL_IDS` to enable.
* Only titles/links are read; no media is downloaded.

---

## 4. Rate limits and politeness

| Service | Limit applied by LankaPulse | On failure |
| --- | --- | --- |
| Publisher RSS | One fetch per feed per cycle (default 180 s) | Logged and retried next cycle |
| Google News RSS | At most one query per locale per cycle | Logged and retried next cycle |
| Nominatim | ≥ 1.1 s between calls, disk cache | Skips geocoding, uses gazetteer |
| Wikipedia | One search + extract per event, cached | Event stored without context |
| Article pages | One fetch per URL, 10 s timeout, `MAX_ITEMS_PER_CYCLE` cap | Headline/summary only |
| GDELT (if enabled) | One query per event | 429 → skip |

The pipeline also pauses `ITEM_PAUSE_SECONDS` (default 1.5 s) between items so a cycle
never bursts requests.

---

## 5. Attribution requirements

If you deploy LankaPulse publicly you must keep:

* the MapLibre attribution control visible (bottom-right) with
  **© OpenStreetMap contributors**, **OpenFreeMap**, **MapLibre**, and
  **geoBoundaries (ODbL)**;
* source outlet names and article links in the event drawer;
* Wikipedia links and CC BY-SA notice where Wikipedia context is shown;
* the **“UI patterns by Rare UI”** credit in the feed panel footer and the
  **“UI patterns: Rare UI”** link in the map attribution (required by Rare UI's
  MIT + Commons Clause + Attribution licence).

ODbL requires that derivative databases remain ODbL. The committed district GeoJSON keeps
its `attribution` and `source` fields for this reason.

---

## 6. Offline / degraded behaviour

| Missing capability | Result |
| --- | --- |
| No internet | UI loads from the local server; previously stored events and markers still render; map tiles fall back to the OSM raster layer if the vector style is unreachable |
| No Ollama / LLM | Automatic switch to the deterministic rule extractor (`extractor: "heuristic"`) |
| Google News blocked | Direct publisher feeds continue; only discovery is reduced |
| Nominatim blocked | Bundled gazetteer + district centroid still place the event |
| Wikipedia blocked | Events are stored without background context |
| WebSocket unavailable | The UI falls back to polling `/api/events` and shows `OFFLINE` |
| Empty database | The UI shows a friendly empty state; `seed_demo.py` adds demo events |

---

## 7. Known limitations

* **Sinhala/Tamil extraction is weaker than English.** Titles are preserved in the original
  script, but category and location inference rely more on the gazetteer and rules.
* **District-centroid fallback is approximate.** The UI labels these points instead of
  pretending they are exact.
* **Dedupe is heuristic.** Title overlap ≥ 0.55 within 12 h and the same district merges
  reports. Different wording of the same incident may remain separate.
* **No historical archive beyond retention.** Raw queue rows are pruned after
  `RAW_ITEM_RETENTION_DAYS`; events remain until deactivated.
* **Google News is not an official API.** Its structure can change without notice.
* **Single-node SQLite.** Fine for one process and modest volume; a multi-worker deployment
  should move to PostgreSQL (SQLAlchemy URL change) and schedule the pipeline separately.
* **Not fact-checking.** Corroboration means "multiple outlets reported it", not "it is true".

---

## 8. Privacy and security

* No analytics, tracking pixels or third-party cookies.
* No user accounts; the app stores no personal data.
* Article text and headlines are sent only to a **local** LLM endpoint when one is enabled.
* `ADMIN_TOKEN` protects the manual trigger endpoint; leave it empty for a local demo.
* CORS defaults to `*` for local development — restrict `CORS_ORIGINS` in production.

---

## 9. Swapping components

| Want to change… | Do this |
| --- | --- |
| LLM | Set `LLM_PROVIDER=openai_compatible` + `LLM_BASE_URL` (llama.cpp, vLLM, LM Studio…) |
| Database | Set `DATABASE_URL` to PostgreSQL/MySQL and install the driver |
| Map tiles | Edit `OPENFREEMAP_STYLE` in `frontend/app.js`; the OSM raster fallback is built in |
| Geocoder | Replace `geocode_nominatim()` in `backend/geocoder.py`, or self-host Nominatim |
| Feed list | Edit `RSS_FEEDS`, `GOOGLE_NEWS_FEEDS` and `TRUSTED_DOMAINS` in `backend/ingestor.py` |
| Boundaries | Run `python scripts/fetch_boundaries.py` |

When adding a source, check its licence and terms first, keep the request rate low, and add
it to `/api/sources` so the provenance stays transparent.
