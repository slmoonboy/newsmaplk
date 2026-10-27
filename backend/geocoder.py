"""
LankaPulse — offline-first geocoder.

Resolution order:
1. Local gazetteer (exact / fuzzy town match)  -> "exact"
2. OpenStreetMap Nominatim (rate-limited)       -> "approximate"
3. District centroid                             -> "district"
4. Country centroid                              -> "country"

Nominatim is free and open data; its usage policy requires at most
one request per second and a descriptive User-Agent, both enforced here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Optional

import httpx

from config import BASE_DIR

logger = logging.getLogger("lankapulse.geocoder")

GAZETTEER_PATH = BASE_DIR / "sri_lanka_gazetteer.json"
CACHE_PATH = BASE_DIR / ".cache" / "nominatim.json"

COUNTRY_CENTER = (7.8731, 80.7718)
COUNTRY_BBOX = (5.80, 79.50, 9.90, 81.95)  # south, west, north, east
_SRI_LANKA_RE = re.compile(r"\b(sri\s*lanka|lanka|ceylon)\b", re.I)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_MIN_INTERVAL = 1.1  # seconds, policy: max 1 req/sec
NOMINATIM_UA = "LankaPulse/1.1 (open-source Sri Lanka news map; low-volume)"

Precision = Literal["exact", "approximate", "district", "country"]

_PUNCT_RE = re.compile(r"[^0-9a-z\u0b80-\u0bff\u0d80-\u0dff ]+")
_SPACE_RE = re.compile(r"\s+")

# Words that add noise to place matching ("Colombo Town" -> "colombo")
_SUFFIXES = (
    " new town",
    " town",
    " city",
    " junction",
    " village",
    " mawatha",
    " road",
    " ds division",
    " pradeshiya sabha",
    " municipal council",
    " urban council",
)


@dataclass(frozen=True)
class GeoResult:
    latitude: float
    longitude: float
    precision: Precision
    matched_name: str = ""

    @property
    def is_approximate(self) -> bool:
        return self.precision != "exact"


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", (value or "").strip())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("&", " and ").lower()
    value = _PUNCT_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", value).strip()


def _strip_suffix(value: str) -> str:
    for suffix in _SUFFIXES:
        if value.endswith(suffix):
            return value[: -len(suffix)].strip()
    return value


@lru_cache(maxsize=1)
def _load_gazetteer() -> dict:
    with open(GAZETTEER_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _build_indexes() -> tuple[dict, dict, list[dict], list[dict]]:
    gaz = _load_gazetteer()
    districts: list[dict] = gaz.get("districts", [])
    towns: list[dict] = gaz.get("towns", [])

    district_index = {_norm(d["name"]): d for d in districts}

    town_index: dict[str, dict] = {}
    for town in towns:
        for alias in {_norm(town["name"]), _strip_suffix(_norm(town["name"]))}:
            if alias:
                town_index.setdefault(alias, town)

    # Longest aliases first so "Colombo Fort" beats "Colombo".
    town_aliases = sorted(town_index.items(), key=lambda kv: len(kv[0]), reverse=True)
    district_aliases = sorted(district_index.items(), key=lambda kv: len(kv[0]), reverse=True)
    return town_index, district_index, town_aliases, district_aliases


@lru_cache(maxsize=1)
def _indexes():
    return _build_indexes()


def district_names() -> list[str]:
    return [d["name"] for d in _load_gazetteer().get("districts", [])]


def district_centroid(district: str) -> Optional[GeoResult]:
    _, district_index, _, _ = _indexes()
    entry = district_index.get(_norm(district))
    if not entry:
        return None
    return GeoResult(entry["lat"], entry["lng"], "district", entry["name"])


def resolve_district(candidate: str = "", town: str = "", text: str = "") -> str:
    """Normalise an LLM-provided district, inferring it from town/text if needed."""
    _, district_index, _, district_aliases = _indexes()
    norm = _norm(candidate)
    if norm in district_index:
        return district_index[norm]["name"]

    # Trim common noise: "Colombo District"
    norm = _norm(re.sub(r"\bdistrict\b", "", candidate, flags=re.I))
    if norm in district_index:
        return district_index[norm]["name"]

    if town:
        town_norm = _norm(town)
        town_index, _, _, _ = _indexes()
        entry = town_index.get(town_norm) or town_index.get(_strip_suffix(town_norm))
        if entry:
            return entry.get("district", "")

    if text:
        found = find_district(text)
        if found:
            return found

    if norm:
        for alias, entry in district_aliases:
            if len(alias) >= 4 and (alias in norm or norm in alias):
                return entry["name"]
    return ""


def find_district(text: str) -> str:
    """Return the Sri Lankan district mentioned earliest in free text.

    An explicit "<name> district" mention wins over a bare place name, which
    avoids mistakes such as "Kandy Road" being read as Kandy district.
    """
    if not text:
        return ""
    norm = f" {_norm(text)} "
    _, _, _, district_aliases = _indexes()

    best: Optional[tuple[int, int, str]] = None
    for alias, entry in district_aliases:
        match = re.search(rf"\b{re.escape(alias)}\s+district\b", norm)
        if not match:
            continue
        candidate = (match.start(), -len(alias), entry["name"])
        if best is None or candidate < best:
            best = candidate

    if best:
        return best[2]

    for alias, entry in district_aliases:
        match = re.search(rf"\b{re.escape(alias)}\b", norm)
        if not match:
            continue
        candidate = (match.start(), -len(alias), entry["name"])
        if best is None or candidate < best:
            best = candidate
    return best[2] if best else ""


def town_district(town: str) -> str:
    """Return the gazetteer district for a town name, or an empty string."""
    if not town:
        return ""
    town_index, _, _, _ = _indexes()
    norm = _norm(town)
    entry = town_index.get(norm) or town_index.get(_strip_suffix(norm))
    return entry.get("district", "") if entry else ""


def sri_lanka_signal_score(text: str) -> int:
    """2 = named Sri Lankan place, 1 = only a country reference, 0 = neither."""
    if not text:
        return 0
    if find_district(text) or find_town(text):
        return 2
    if _SRI_LANKA_RE.search(text):
        return 1
    return 0


def text_has_sri_lanka_signal(text: str) -> bool:
    """Cheap pre-screen: does this item look like it is about Sri Lanka?

    Used to prioritise the queue and to avoid spending network calls on world
    news that a Sri Lankan publisher happened to syndicate.
    """
    return sri_lanka_signal_score(text) > 0


def find_town(text: str, district: str = "") -> Optional[dict]:
    """Return the most specific gazetteer town mentioned in free text."""
    if not text:
        return None
    norm = f" {_norm(text)} "
    town_index, _, town_aliases, _ = _indexes()
    wanted = _norm(district)

    best: Optional[tuple[int, int, dict]] = None
    for alias, entry in town_aliases:
        if len(alias) < 3:
            continue
        if not re.search(rf"\b{re.escape(alias)}\b", norm):
            continue
        if wanted and _norm(entry.get("district", "")) != wanted:
            continue
        position = norm.find(alias)
        candidate = (len(alias), -position, entry)
        if best is None or candidate[:2] > best[:2]:
            best = candidate

    if best:
        return best[2]

    # If the district did not match, retry globally.
    if wanted:
        return find_town(text)
    return None


def geocode_local(place_name: str, district: str = "") -> Optional[GeoResult]:
    """Gazetteer lookup only (no network)."""
    if not place_name and not district:
        return None

    town_index, district_index, _, _ = _indexes()
    wanted = _norm(district)

    def _find(key: str) -> Optional[dict]:
        entry = town_index.get(key)
        if entry and (not wanted or _norm(entry.get("district", "")) == wanted):
            return entry
        return None

    norm = _norm(place_name)
    if norm:
        entry = _find(norm) or _find(_strip_suffix(norm))
        if entry:
            return GeoResult(entry["lat"], entry["lng"], "exact", entry["name"])

    # District name used as the location -> district centroid.
    if norm in district_index:
        entry = district_index[norm]
        return GeoResult(entry["lat"], entry["lng"], "district", entry["name"])

    # Containment, longest alias first.
    if norm and len(norm) >= 4:
        for alias, entry in sorted(town_index.items(), key=lambda kv: len(kv[0]), reverse=True):
            if wanted and _norm(entry.get("district", "")) != wanted:
                continue
            if len(alias) >= 4 and (alias in norm or norm in alias):
                return GeoResult(entry["lat"], entry["lng"], "exact", entry["name"])

    if wanted and wanted in district_index:
        entry = district_index[wanted]
        return GeoResult(entry["lat"], entry["lng"], "district", entry["name"])

    return None


# ─── Nominatim (free / open data, rate-limited) ───────────────

_nominatim_lock = asyncio.Lock()
_last_nominatim_call = 0.0
_disk_cache: Optional[dict[str, list[float]]] = None
_memory_cache: dict[str, Optional[tuple[float, float]]] = {}


def _load_disk_cache() -> dict[str, list[float]]:
    global _disk_cache
    if _disk_cache is not None:
        return _disk_cache
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
            _disk_cache = {k: v for k, v in raw.items() if isinstance(v, list) and len(v) == 2}
    except (OSError, ValueError):
        _disk_cache = {}
    return _disk_cache


def _save_disk_cache() -> None:
    if _disk_cache is None:
        return
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_disk_cache, fh)
    except OSError as exc:  # pragma: no cover - non-critical
        logger.debug("Could not write Nominatim cache: %s", exc)


def _within_bbox(lat: float, lon: float) -> bool:
    south, west, north, east = COUNTRY_BBOX
    return south <= lat <= north and west <= lon <= east


async def geocode_nominatim(place_name: str, district: str = "") -> Optional[GeoResult]:
    global _last_nominatim_call

    place_name = (place_name or "").strip()
    if not place_name:
        return None

    key = _norm(f"{place_name} {district}")
    if key in _memory_cache:
        cached = _memory_cache[key]
        return GeoResult(cached[0], cached[1], "approximate", place_name) if cached else None

    cache = _load_disk_cache()
    if key in cache:
        value = cache[key]
        _memory_cache[key] = (value[0], value[1])
        return GeoResult(value[0], value[1], "approximate", place_name)

    query = f"{place_name}, Sri Lanka"
    if district:
        query = f"{place_name}, {district} District, Sri Lanka"

    async with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _last_nominatim_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    NOMINATIM_URL,
                    params={
                        "q": query,
                        "format": "jsonv2",
                        "limit": 1,
                        "countrycodes": "lk",
                        "addressdetails": 0,
                    },
                    headers={"User-Agent": NOMINATIM_UA},
                )
                response.raise_for_status()
                results = response.json()
        except Exception as exc:
            logger.warning("Nominatim lookup failed for '%s': %s", place_name, exc)
            _memory_cache[key] = None
            return None
        finally:
            _last_nominatim_call = time.monotonic()

    if not results:
        _memory_cache[key] = None
        return None

    try:
        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
    except (KeyError, TypeError, ValueError):
        _memory_cache[key] = None
        return None

    if not _within_bbox(lat, lon):
        _memory_cache[key] = None
        return None

    _memory_cache[key] = (lat, lon)
    cache[key] = [lat, lon]
    _save_disk_cache()
    return GeoResult(lat, lon, "approximate", results[0].get("display_name", place_name))


async def geocode(place_name: str, district: str = "") -> GeoResult:
    """Geocode a place, degrading gracefully instead of guessing silently."""
    local = geocode_local(place_name, district)
    if local and local.precision == "exact":
        return local

    # Try Nominatim for towns the local gazetteer does not contain, unless the
    # place is already a district name (centroid is accurate enough there).
    is_district_name = _norm(place_name) in _indexes()[1]
    if place_name and not is_district_name:
        approximate = await geocode_nominatim(place_name, district)
        if approximate:
            return approximate

    if local:
        return local

    centroid = district_centroid(district)
    if centroid:
        return centroid

    return GeoResult(*COUNTRY_CENTER, "country", "Sri Lanka")
