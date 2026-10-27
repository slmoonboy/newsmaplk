"""
LankaPulse — free/open corroboration ("grounding").

Given a headline, this module looks for independent coverage using:

* Google News RSS search (public RSS, filtered to trusted publishers)
* Wikipedia / Wikidata summaries (CC BY-SA)
* optionally GDELT DOC 2.0 (open data, strict rate limits)

It replaces the original Playwright/Google-Search scraper: no browser, no
API keys, no ToS-violating scraping. All providers are optional and failures
degrade to an empty bundle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

import feedparser
import httpx
from bs4 import BeautifulSoup

from config import Settings
from ingestor import (
    USER_AGENT,
    clean_html,
    extract_domain,
    is_trusted_domain,
    looks_like_spam,
)

logger = logging.getLogger("lankapulse.grounder")

GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "from", "with", "by", "as", "is", "are", "was", "were", "be", "been", "has",
    "have", "had", "will", "would", "can", "could", "says", "said", "after",
    "before", "over", "under", "into", "about", "than", "this", "that", "these",
    "those", "his", "her", "its", "their", "our", "your", "more", "most", "not",
    "no", "amid", "during", "while", "sri", "lanka",
}

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class GroundedSource:
    title: str
    snippet: str
    url: str
    source_domain: str
    provider: str = "google_news"
    published_at: Optional[datetime] = None
    full_text: str = ""


@dataclass
class GroundedContextBundle:
    query: str
    sources: list[GroundedSource] = field(default_factory=list)
    combined_context: str = ""
    providers: list[str] = field(default_factory=list)
    excluded_domain: str = ""

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def news_sources(self) -> list[GroundedSource]:
        """Independent news coverage (Wikipedia is background, not corroboration)."""
        return [s for s in self.sources if s.provider in {"google_news", "gdelt"}]

    @property
    def news_source_count(self) -> int:
        return len(self.news_sources)

    @property
    def is_corroborated(self) -> bool:
        return self.news_source_count >= 2

    @property
    def independent_domains(self) -> list[str]:
        return sorted({s.source_domain for s in self.sources if s.source_domain})


EMPTY_BUNDLE = GroundedContextBundle(query="", combined_context="")

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_cache: dict[str, tuple[float, GroundedContextBundle]] = {}
_gdelt_lock = asyncio.Lock()
_gdelt_last_call = 0.0
_gdelt_blocked_until = 0.0


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=httpx.Timeout(12.0, connect=6.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return _client


async def shutdown() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ─── Query construction ───────────────────────────────────────

def keywords(query: str, limit: int = 9) -> list[str]:
    words = _TOKEN_RE.findall(query or "")
    selected: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in STOPWORDS or len(lowered) < 3:
            continue
        if word not in selected:
            selected.append(word)
        if len(selected) >= limit:
            break
    return selected


def _locale(language: str) -> tuple[str, str, str]:
    language = (language or "EN").upper()
    if language == "SI":
        return "si", "LK", "LK:si"
    if language == "TA":
        return "ta", "LK", "LK:ta"
    return "en-LK", "LK", "LK:en"


# ─── Google News RSS ─────────────────────────────────────────

async def _google_news_search(
    title: str,
    language: str,
    exclude_domain: str,
    max_results: int,
) -> list[GroundedSource]:
    terms = keywords(title, limit=9)
    if not terms:
        return []

    client = await _get_client()
    hl, gl, ceid = _locale(language)

    for query_terms in (terms, terms[:5]):
        params = {
            "q": f"{' '.join(query_terms)} when:3d",
            "hl": hl,
            "gl": gl,
            "ceid": ceid,
        }
        try:
            response = await client.get(GOOGLE_NEWS_URL, params=params)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception as exc:
            logger.debug("Google News RSS search failed: %s", exc)
            continue

        results: list[GroundedSource] = []
        seen: set[str] = set()
        query_tokens = {term.lower() for term in terms}
        for entry in feed.entries[:40]:
            entry_title = clean_html(entry.get("title", ""), limit=400)
            if not entry_title or looks_like_spam(entry_title):
                continue

            source_meta = entry.get("source") if isinstance(entry.get("source"), dict) else {}
            domain = extract_domain(source_meta.get("href", "")) or extract_domain(entry.get("link", ""))
            if not domain or not is_trusted_domain(f"https://{domain}"):
                continue
            if exclude_domain and (domain == exclude_domain or domain.endswith("." + exclude_domain)):
                continue

            publisher = clean_html(source_meta.get("title", ""), limit=120)
            display_title = entry_title
            if publisher and display_title.endswith(f" - {publisher}"):
                display_title = display_title[: -(len(publisher) + 3)].strip()

            # Relevance guard: generic homepage/aggregator titles are dropped.
            result_tokens = {t.lower() for t in _TOKEN_RE.findall(display_title)}
            if not (query_tokens & result_tokens):
                continue

            key = display_title.lower()
            if key in seen:
                continue
            seen.add(key)

            published = None
            parsed = entry.get("published_parsed")
            if parsed:
                try:
                    published = datetime(*parsed[:6])
                except (TypeError, ValueError):
                    published = None

            results.append(GroundedSource(
                title=display_title,
                snippet=clean_html(entry.get("summary", ""), limit=600),
                url=entry.get("link", ""),
                source_domain=domain,
                provider="google_news",
                published_at=published,
            ))
            if len(results) >= max_results:
                break

        if results:
            return results

    return []


# ─── Wikipedia ────────────────────────────────────────────────

async def _wikipedia_search(title: str, language: str) -> list[GroundedSource]:
    lang = {"SI": "si", "TA": "ta"}.get((language or "EN").upper(), "en")
    terms = keywords(title, limit=6)
    if not terms:
        return []

    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{' '.join(terms)} Sri Lanka",
        "gsrlimit": 2,
        "prop": "extracts|info",
        "exintro": 1,
        "explaintext": 1,
        "inprop": "url",
        "format": "json",
        "formatversion": 2,
    }

    try:
        client = await _get_client()
        response = await client.get(WIKI_API.format(lang=lang), params=params, timeout=8.0)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.debug("Wikipedia search failed: %s", exc)
        return []

    pages = (data.get("query") or {}).get("pages") or []
    query_tokens = {t.lower() for t in terms}
    sources: list[GroundedSource] = []

    for page in pages:
        extract = clean_html(page.get("extract", ""), limit=900)
        page_title = page.get("title", "")
        if len(extract) < 80 or not page_title:
            continue
        title_tokens = {t.lower() for t in _TOKEN_RE.findall(page_title)}
        # Keep only pages that actually overlap the query; avoids generic hits.
        if not (query_tokens & title_tokens) and not any(
            token in extract.lower()[:400] for token in query_tokens
        ):
            continue

        sources.append(GroundedSource(
            title=page_title,
            snippet=extract,
            url=page.get("fullurl", f"https://{lang}.wikipedia.org/wiki/{page_title.replace(' ', '_')}"),
            source_domain=f"{lang}.wikipedia.org",
            provider="wikipedia",
        ))

    return sources


# ─── GDELT (optional, heavily rate-limited) ───────────────────

async def _gdelt_search(title: str, exclude_domain: str, max_results: int) -> list[GroundedSource]:
    global _gdelt_last_call, _gdelt_blocked_until

    now = time.monotonic()
    if now < _gdelt_blocked_until:
        return []

    terms = keywords(title, limit=6)
    if not terms:
        return []

    async with _gdelt_lock:
        wait = 5.2 - (time.monotonic() - _gdelt_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            client = await _get_client()
            response = await client.get(
                GDELT_URL,
                params={
                    "query": f"{' '.join(terms)} sourcecountry:LK",
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": max_results + 3,
                    "sort": "datedesc",
                },
                timeout=10.0,
            )
            if response.status_code == 429:
                _gdelt_blocked_until = time.monotonic() + 600
                logger.info("GDELT rate limit hit; pausing GDELT for 10 minutes")
                return []
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.debug("GDELT search failed: %s", exc)
            return []
        finally:
            _gdelt_last_call = time.monotonic()

    sources: list[GroundedSource] = []
    for article in data.get("articles", [])[:max_results]:
        url = article.get("url", "")
        domain = extract_domain(url)
        if not domain or not is_trusted_domain(url):
            continue
        if exclude_domain and (domain == exclude_domain or domain.endswith("." + exclude_domain)):
            continue
        sources.append(GroundedSource(
            title=clean_html(article.get("title", ""), limit=300),
            snippet="",
            url=url,
            source_domain=domain,
            provider="gdelt",
        ))
    return sources


# ─── Article extraction (httpx + BeautifulSoup, no browser) ───

_ARTICLE_SELECTORS = (
    "article",
    "[itemprop='articleBody']",
    ".article-body",
    ".story-body",
    ".entry-content",
    ".post-content",
    ".article-content",
    ".news-content",
    "main",
)


async def fetch_article_text(url: str, max_chars: int = 2200) -> str:
    if not url or not url.startswith("http") or "news.google.com" in url:
        return ""
    if url.lower().endswith((".pdf", ".mp4", ".mp3")):
        return ""

    try:
        client = await _get_client()
        response = await client.get(url, timeout=8.0)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and content_type:
            return ""

        soup = BeautifulSoup(response.text[:400_000], "lxml")
        for tag in soup(["script", "style", "nav", "footer", "aside", "form", "noscript"]):
            tag.decompose()

        node = None
        for selector in _ARTICLE_SELECTORS:
            candidate = soup.select_one(selector)
            if candidate and len(candidate.get_text(" ", strip=True)) > 200:
                node = candidate
                break

        text = (node or soup.body or soup).get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as exc:
        logger.debug("Article extraction failed for %s: %s", url, exc)
        return ""


# ─── Public API ───────────────────────────────────────────────

def _dedupe_sources(sources: Iterable[GroundedSource], limit: int) -> list[GroundedSource]:
    output: list[GroundedSource] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for source in sources:
        if not source.title:
            continue
        title_key = re.sub(r"\W+", "", source.title.lower())[:90]
        url_key = source.url.split("?")[0]
        if url_key in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        output.append(source)
        if len(output) >= limit:
            break
    return output


def _build_context(sources: list[GroundedSource], query: str) -> str:
    parts = [f"Search query: {query}"]
    for source in sources:
        block = (
            f"Provider: {source.provider}\n"
            f"Publisher: {source.source_domain}\n"
            f"Title: {source.title}"
        )
        if source.snippet:
            block += f"\nSnippet: {source.snippet}"
        if source.full_text:
            block += f"\nArticle excerpt: {source.full_text}"
        parts.append(block)
    return "\n\n---\n\n".join(parts)[:6000]


async def ground_event(
    title: str,
    description: str = "",
    language: str = "EN",
    exclude_domain: str = "",
    primary_url: str = "",
    settings: Optional[Settings] = None,
) -> GroundedContextBundle:
    """Find independent coverage for a headline. Never raises."""
    if settings is None:
        from config import settings as global_settings
        settings = global_settings

    if not settings.enable_grounding or not (title or "").strip():
        return GroundedContextBundle(query=title or "")

    cache_key = f"{title.strip().lower()}|{language}|{exclude_domain}|{primary_url}"
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    # Providers run concurrently; a slow one cannot block the others.
    tasks = [
        asyncio.create_task(_google_news_search(title, language, exclude_domain, settings.grounding_max_sources)),
        asyncio.create_task(_wikipedia_search(title, language)),
    ]
    if settings.enable_gdelt:
        tasks.append(asyncio.create_task(_gdelt_search(title, exclude_domain, 3)))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    collected: list[GroundedSource] = []
    for result in results:
        if isinstance(result, list):
            collected.extend(result)
        elif isinstance(result, Exception):
            logger.debug("Grounding provider error: %s", result)

    sources = _dedupe_sources(collected, settings.grounding_max_sources)

    # Add the original article's full text when its URL is a direct publisher
    # link (Google News redirects are skipped). This is context, not a second
    # independent source, so it is labelled "primary" and excluded from counts.
    if settings.enable_article_fetch and primary_url and "news.google.com" not in primary_url:
        full_text = await fetch_article_text(primary_url)
        if full_text:
            sources.insert(0, GroundedSource(
                title=title,
                snippet=description[:600],
                url=primary_url,
                source_domain=exclude_domain or extract_domain(primary_url),
                provider="primary",
                full_text=full_text,
            ))
            sources = _dedupe_sources(sources, settings.grounding_max_sources + 1)

    providers = sorted({source.provider for source in sources})
    bundle = GroundedContextBundle(
        query=title,
        sources=sources,
        combined_context=_build_context(sources, title),
        providers=providers,
        excluded_domain=exclude_domain,
    )

    if settings.grounding_cache_seconds:
        _cache[cache_key] = (now + settings.grounding_cache_seconds, bundle)
        # Keep the cache bounded.
        if len(_cache) > 500:
            for key in list(_cache)[:100]:
                _cache.pop(key, None)

    logger.info(
        "Grounding '%s': %d sources (%d independent) from %s",
        title[:60], bundle.source_count, bundle.news_source_count,
        ", ".join(providers) or "none",
    )
    return bundle
