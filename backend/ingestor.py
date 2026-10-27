"""
LankaPulse — ingestion engine.

Only free/open sources are used:
* publisher RSS feeds
* Google News RSS search (filtered to trusted publisher domains)
* optional YouTube channel RSS feeds

No HTML scraping of search engines, no API keys, no paid services.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("lankapulse.ingestor")

USER_AGENT = "LankaPulse/1.1 (+https://github.com/; open-source news map)"

# ─── Publisher RSS feeds (all public, no keys) ────────────────

RSS_FEEDS: list[dict] = [
    {"name": "Ada Derana", "url": "https://www.adaderana.lk/rss.php", "language": "EN"},
    {"name": "Ada Derana Sinhala", "url": "https://sinhala.adaderana.lk/rss.php", "language": "SI"},
    {"name": "EconomyNext", "url": "https://economynext.com/feed/", "language": "EN"},
    {"name": "The Island", "url": "https://island.lk/feed/", "language": "EN"},
    {"name": "BBC Sinhala", "url": "https://feeds.bbci.co.uk/sinhala/rss.xml", "language": "SI"},
    {"name": "BBC Tamil", "url": "https://feeds.bbci.co.uk/tamil/rss.xml", "language": "TA"},
]

# Google News RSS searches. Results are kept only when the publisher domain is
# in TRUSTED_DOMAINS, which removes the live-stream/SEO spam that pollutes
# generic Sri Lanka queries.
GOOGLE_NEWS_FEEDS: list[dict] = [
    {
        "name": "Google News (EN)",
        "url": "https://news.google.com/rss/search",
        "query": "Sri Lanka breaking news when:2h",
        "language": "EN",
        "locale": ("en-LK", "LK", "LK:en"),
    },
    {
        "name": "Google News (EN incidents)",
        "url": "https://news.google.com/rss/search",
        "query": "Sri Lanka accident OR flood OR protest OR crime when:3h",
        "language": "EN",
        "locale": ("en-LK", "LK", "LK:en"),
    },
    {
        "name": "Google News (SI)",
        "url": "https://news.google.com/rss/search",
        "query": "ශ්‍රී ලංකා when:2h",
        "language": "SI",
        "locale": ("si", "LK", "LK:si"),
    },
    {
        "name": "Google News (TA)",
        "url": "https://news.google.com/rss/search",
        "query": "இலங்கை when:2h",
        "language": "TA",
        "locale": ("ta", "LK", "LK:ta"),
    },
    {
        "name": "Google News (Daily Mirror)",
        "url": "https://news.google.com/rss/search",
        "query": "site:dailymirror.lk when:3h",
        "language": "EN",
        "locale": ("en-LK", "LK", "LK:en"),
    },
    {
        "name": "Google News (NewsFirst)",
        "url": "https://news.google.com/rss/search",
        "query": "site:newsfirst.lk when:3h",
        "language": "EN",
        "locale": ("en-LK", "LK", "LK:en"),
    },
    {
        "name": "Google News (Daily FT)",
        "url": "https://news.google.com/rss/search",
        "query": "site:ft.lk when:3h",
        "language": "EN",
        "locale": ("en-LK", "LK", "LK:en"),
    },
]

TRUSTED_DOMAINS: tuple[str, ...] = (
    # Sri Lankan outlets
    "adaderana.lk",
    "dailymirror.lk",
    "newsfirst.lk",
    "economynext.com",
    "island.lk",
    "ft.lk",
    "dailynews.lk",
    "sundaytimes.lk",
    "hirunews.lk",
    "colombotelegraph.com",
    "colombopage.com",
    "lankabusinessonline.com",
    "themorning.lk",
    "ceylontoday.lk",
    "nation.lk",
    "observer.lk",
    "news.lk",
    "slbc.lk",
    "itn.lk",
    "rupavahini.lk",
    "lankasara.com",
    "tamilmirror.lk",
    "thinakaran.lk",
    # International wires
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "aljazeera.com",
    "channelnewsasia.com",
    "xinhuanet.com",
    "thehindu.com",
    "newindianexpress.com",
)

_SPAM_RE = re.compile(
    r"(live\s*(stream|coverage|update|match)|watch\s+live|free\s+stream|"
    r"\bodds\b|\bbetting\b|prediction|scorecard|match\s+preview|"
    r"how\s+to\s+watch|tv\s+channel\s+link|download\s+link)",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
}

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


# ─── Data model ───────────────────────────────────────────────

@dataclass
class RawNewsItem:
    title: str
    summary: str = ""
    url: str = ""
    source_name: str = ""
    platform: str = "RSS"  # RSS | WEB | X_POST | YOUTUBE
    language: str = "EN"
    published_at: Optional[datetime] = None
    thumbnail_url: str = ""
    author_handle: str = ""
    provider: str = "rss"
    extra: dict = field(default_factory=dict)

    @property
    def url_hash(self) -> str:
        key = canonical_url(self.url) or f"{self.title}|{self.source_name}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ─── Helpers ──────────────────────────────────────────────────

def canonical_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() not in _TRACKING_PARAMS]
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(query),
            "",
        ))
    except ValueError:
        return url


def extract_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def is_trusted_domain(url: str) -> bool:
    domain = extract_domain(url)
    if not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in TRUSTED_DOMAINS)


def clean_html(value: str, limit: int = 1200) -> str:
    if not value:
        return ""
    text = unescape(value)
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    else:
        text = _TAG_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text[:limit]


def looks_like_spam(title: str) -> bool:
    if not title or len(title) < 12:
        return True
    if _SPAM_RE.search(title):
        return True
    weird = 0
    for ch in title:
        if ch.isalnum() or ch.isspace() or ch in ".,'\"()-:;/&%$+!?":
            continue
        # Letters/marks/digits from any script (Sinhala and Tamil vowel signs
        # are combining marks, not alphanumeric).
        if unicodedata.category(ch)[0] in {"L", "M", "N"}:
            continue
        weird += 1
    if weird / max(len(title), 1) > 0.12:
        return True
    return False


def _parse_struct_time(parsed) -> Optional[datetime]:
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _entry_datetime(entry) -> Optional[datetime]:
    return (
        _parse_struct_time(entry.get("published_parsed"))
        or _parse_struct_time(entry.get("updated_parsed"))
    )


def _strip_publisher_suffix(title: str, publisher: str = "") -> str:
    title = (title or "").strip()
    if publisher:
        for sep in (" - ", " | ", " – "):
            suffix = f"{sep}{publisher}"
            if title.endswith(suffix):
                return title[: -len(suffix)].strip()
    return title


async def get_http_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en,si,ta"},
                follow_redirects=True,
                timeout=httpx.Timeout(20.0, connect=10.0),
                limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
            )
        return _client


async def close_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ─── Feed fetching ────────────────────────────────────────────

async def _fetch_feed(config: dict, client: httpx.AsyncClient) -> list[RawNewsItem]:
    items: list[RawNewsItem] = []
    try:
        params = {}
        if "locale" in config:
            hl, gl, ceid = config["locale"]
            params = {"q": config["query"], "hl": hl, "gl": gl, "ceid": ceid}

        response = await client.get(config["url"], params=params or None)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            logger.warning("Feed parse failed for %s", config["name"])
            return items

        for entry in feed.entries[:40]:
            title = clean_html(entry.get("title", ""), limit=400)
            if not title:
                continue

            publisher = ""
            domain = ""
            source = entry.get("source")
            if isinstance(source, dict):
                publisher = clean_html(source.get("title", ""), limit=120)
                domain = extract_domain(source.get("href", "")) or extract_domain(entry.get("link", ""))
            else:
                domain = extract_domain(entry.get("link", ""))
                publisher = config["name"]

            is_google = "news.google.com" in config["url"]
            if is_google:
                if not domain or not is_trusted_domain(f"https://{domain}"):
                    continue
                title = _strip_publisher_suffix(title, publisher)

            if looks_like_spam(title):
                continue

            summary = clean_html(
                entry.get("summary", entry.get("description", "")), limit=1200
            )

            items.append(RawNewsItem(
                title=title,
                summary=summary,
                url=entry.get("link", ""),
                source_name=publisher or config["name"],
                platform="RSS",
                language=config.get("language", "EN"),
                published_at=_entry_datetime(entry) or datetime.utcnow(),
                provider="google_news" if is_google else "rss",
                extra={"domain": domain},
            ))
    except Exception as exc:
        logger.warning("Feed fetch failed for %s: %s", config["name"], exc)
    return items


async def fetch_all_rss() -> list[RawNewsItem]:
    client = await get_http_client()
    semaphore = asyncio.Semaphore(6)

    async def _guarded(config: dict) -> list[RawNewsItem]:
        async with semaphore:
            return await _fetch_feed(config, client)

    results = await asyncio.gather(
        *(_guarded(config) for config in [*RSS_FEEDS, *GOOGLE_NEWS_FEEDS]),
        return_exceptions=True,
    )

    items: list[RawNewsItem] = []
    for result in results:
        if isinstance(result, list):
            items.extend(result)
        elif isinstance(result, Exception):
            logger.warning("Feed batch error: %s", result)

    logger.info("RSS ingestion produced %d candidate items", len(items))
    return items


async def fetch_youtube_rss(channel_ids: Iterable[str]) -> list[RawNewsItem]:
    """Fetch public YouTube channel RSS feeds (free, no API key)."""
    channel_ids = [cid.strip() for cid in channel_ids if cid.strip()]
    if not channel_ids:
        return []

    client = await get_http_client()
    items: list[RawNewsItem] = []

    async def _one(channel_id: str) -> None:
        try:
            response = await client.get(
                "https://www.youtube.com/feeds/videos.xml",
                params={"channel_id": channel_id},
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            author = clean_html(feed.feed.get("title", "YouTube"), limit=120)
            for entry in feed.entries[:8]:
                title = clean_html(entry.get("title", ""), limit=400)
                if not title or looks_like_spam(title):
                    continue
                thumbnail = ""
                media = entry.get("media_thumbnail") or []
                if media:
                    thumbnail = media[0].get("url", "")
                items.append(RawNewsItem(
                    title=title,
                    summary=clean_html(entry.get("summary", ""), limit=800),
                    url=entry.get("link", ""),
                    source_name=author or "YouTube",
                    platform="YOUTUBE",
                    language="EN",
                    published_at=_entry_datetime(entry) or datetime.utcnow(),
                    thumbnail_url=thumbnail,
                    provider="youtube_rss",
                ))
        except Exception as exc:
            logger.warning("YouTube RSS failed for channel %s: %s", channel_id, exc)

    await asyncio.gather(*(_one(cid) for cid in channel_ids))
    return items


async def run_ingestion_cycle(youtube_channel_ids: Iterable[str] = ()) -> list[RawNewsItem]:
    """Fetch every source once and deduplicate within the batch."""
    rss_items, yt_items = await asyncio.gather(
        fetch_all_rss(),
        fetch_youtube_rss(youtube_channel_ids),
    )

    seen: set[str] = set()
    unique: list[RawNewsItem] = []
    for item in [*rss_items, *yt_items]:
        if item.url_hash in seen:
            continue
        seen.add(item.url_hash)
        unique.append(item)

    unique.sort(key=lambda i: i.published_at or datetime.min, reverse=True)
    logger.info("Ingestion cycle complete: %d unique items", len(unique))
    return unique


def source_catalog() -> list[dict]:
    """Describe ingestion sources for the /api/sources endpoint."""
    catalog = [
        {
            "name": feed["name"],
            "url": feed["url"],
            "language": feed["language"],
            "platform": "RSS",
            "provider": "rss",
        }
        for feed in RSS_FEEDS
    ]
    catalog.extend(
        {
            "name": feed["name"],
            "url": feed["url"],
            "query": feed["query"],
            "language": feed["language"],
            "platform": "RSS",
            "provider": "google_news",
        }
        for feed in GOOGLE_NEWS_FEEDS
    )
    return catalog
