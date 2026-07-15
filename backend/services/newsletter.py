from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CACHE_DIR  # noqa: E402
from services.net_utils import build_session, disable_dead_proxy_env  # noqa: E402


CACHE_FILE = os.path.join(CACHE_DIR, "newsletter.json")
CACHE_TTL = 1800

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,application/json,text/html,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

RSS_SOURCES = [
    {
        "name": "Federal Reserve",
        "category": "Central Bank",
        "type": "rss",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "limit": 4,
    },
    {
        "name": "ECB",
        "category": "Central Bank",
        "type": "rss",
        "url": "https://www.ecb.europa.eu/rss/press.html",
        "limit": 4,
    },
    {
        "name": "BIS Press",
        "category": "Institution",
        "type": "rss",
        "url": "https://www.bis.org/doclist/all_pressrels.rss",
        "limit": 4,
    },
    {
        "name": "BIS Speeches",
        "category": "Institution",
        "type": "rss",
        "url": "https://www.bis.org/doclist/cbspeeches.rss",
        "limit": 4,
    },
    {
        "name": "CFTC Press",
        "category": "Regulator",
        "type": "rss",
        "url": "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
        "limit": 4,
    },
    {
        "name": "CFTC Speeches",
        "category": "Regulator",
        "type": "rss",
        "url": "https://www.cftc.gov/RSS/RSSST/rssst.xml",
        "limit": 4,
    },
]

JSON_SOURCES = [
    {
        "name": "World Bank",
        "category": "Development Institution",
        "type": "json",
        "url": (
            "https://search.worldbank.org/api/v2/news?"
            "format=json&rows=5&displayconttype_exact=Press%20Release&lang_exact=English&os=0"
        ),
        "limit": 4,
    },
]

HTML_SOURCES = [
    {
        "name": "BlackRock Insights",
        "category": "Asset Manager",
        "type": "html",
        "url": "https://www.blackrock.com/us/financial-professionals/insights",
        "limit": 4,
    },
    {
        "name": "Morgan Stanley Ideas",
        "category": "Bank",
        "type": "html",
        "url": "https://www.morganstanley.com/ideas",
        "limit": 3,
    },
    {
        "name": "Goldman Sachs Insights",
        "category": "Bank",
        "type": "html",
        "url": "https://www.goldmansachs.com/insights/",
        "limit": 3,
    },
]


def _load_cache() -> list[dict[str, Any]] | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        if time.time() - os.path.getmtime(CACHE_FILE) > CACHE_TTL:
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        data = payload.get("data") if isinstance(payload, dict) else payload
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _save_cache(data: list[dict[str, Any]]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "as_of": datetime.now(timezone.utc).isoformat(),
                    "data": data,
                },
                handle,
                default=str,
            )
    except Exception:
        pass


def _session():
    disable_dead_proxy_env()
    session = build_session(headers=HEADERS)
    return session


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(html.unescape(str(value)), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _clean_url(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _parse_date(*values: Any) -> tuple[str, float]:
    for value in values:
        if not value:
            continue
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(), dt.timestamp()
        except Exception:
            pass
        try:
            dt = parsedate_to_datetime(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(), dt.timestamp()
        except Exception:
            pass
    now = datetime.now(timezone.utc)
    return now.isoformat(), now.timestamp()


def _normalize_item(
    *,
    source: str,
    category: str,
    title: Any,
    link: Any,
    summary: Any = None,
    published: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    clean_title = _clean_text(title)
    clean_link = _clean_url(link)
    if not clean_title or not clean_link:
        return None
    published_iso, published_ts = _parse_date(published)
    clean_summary = _clean_text(summary)
    if clean_summary:
        clean_summary = clean_summary[:180].rstrip()
    item = {
        "title": clean_title,
        "source": source,
        "category": category,
        "url": clean_link,
        "published": published_iso,
        "published_ts": published_ts,
        "summary": clean_summary,
        "is_real": True,
    }
    if extra:
        item.update(extra)
    return item


def _parse_rss_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    session = _session()
    try:
        resp = session.get(source["url"], timeout=25)
        resp.raise_for_status()
    except Exception:
        return []

    try:
        root = ET.fromstring(resp.text)
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    nodes = list(root.findall(".//item"))
    if not nodes:
        nodes = list(root.findall(".//{http://www.w3.org/2005/Atom}entry")) or list(root.findall(".//entry"))

    for node in nodes[: int(source.get("limit", 4))]:
        if node.tag.endswith("entry"):
            title = node.findtext("{http://www.w3.org/2005/Atom}title") or node.findtext("title")
            link_node = node.find("{http://www.w3.org/2005/Atom}link") or node.find("link")
            href = ""
            if link_node is not None:
                href = link_node.attrib.get("href") or link_node.text or ""
            summary = node.findtext("{http://www.w3.org/2005/Atom}summary") or node.findtext("summary")
            published = (
                node.findtext("{http://www.w3.org/2005/Atom}updated")
                or node.findtext("updated")
                or node.findtext("{http://www.w3.org/2005/Atom}published")
                or node.findtext("published")
            )
        else:
            title = node.findtext("title")
            href = node.findtext("link")
            summary = node.findtext("description")
            published = node.findtext("pubDate") or node.findtext("date")

        item = _normalize_item(
            source=source["name"],
            category=source["category"],
            title=title,
            link=href,
            summary=summary,
            published=published,
        )
        if item:
            items.append(item)
    return items


def _extract_candidate_cards(soup: BeautifulSoup, base_url: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_item(title: str, url: str, summary: str = "", published: str = "") -> None:
        key = f"{title.lower()}|{url.lower()}"
        if key in seen:
            return
        seen.add(key)
        item = _normalize_item(
            source=source["name"],
            category=source["category"],
            title=title,
            link=urljoin(base_url, url),
            summary=summary,
            published=published,
        )
        if item:
            items.append(item)

    for article in soup.find_all("article"):
        title = ""
        url = ""
        summary = ""
        published = ""

        for heading in article.find_all(["h1", "h2", "h3", "h4"], limit=2):
            title = _clean_text(heading.get_text(" ", strip=True))
            if title:
                break

        link_node = article.find("a", href=True)
        if link_node:
            url = link_node.get("href") or ""
            if not title:
                title = _clean_text(link_node.get_text(" ", strip=True))

        for para in article.find_all("p", limit=2):
            text = _clean_text(para.get_text(" ", strip=True))
            if text and len(text) > len(summary):
                summary = text

        time_node = article.find("time")
        if time_node:
            published = time_node.get("datetime") or time_node.get_text(" ", strip=True)

        if title and url:
            add_item(title, url, summary, published)

    if len(items) >= int(source.get("limit", 4)):
        return items[: int(source.get("limit", 4))]

    for anchor in soup.find_all("a", href=True):
        text = _clean_text(anchor.get_text(" ", strip=True))
        href = anchor.get("href") or ""
        if len(text) < 24 or len(text) > 160:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        if any(bad in href.lower() for bad in ("/contact", "/login", "/careers", "/privacy", "/cookie")):
            continue
        if source["name"].split()[0].lower() not in text.lower() and len(items) > 0:
            continue
        add_item(text, href)
        if len(items) >= int(source.get("limit", 4)):
            break

    return items[: int(source.get("limit", 4))]


def _parse_html_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    session = _session()
    try:
        resp = session.get(source["url"], timeout=30)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = _extract_candidate_cards(soup, source["url"], source)
    return items


def _parse_world_bank_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    session = _session()
    try:
        resp = session.get(source["url"], timeout=25)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return []

    candidates: list[dict[str, Any]] = []
    for key in ("data", "news", "results", "items"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            candidates = value
            break
    if not candidates and isinstance(payload, dict):
        maybe = payload.get("result")
        if isinstance(maybe, dict):
            for key in ("data", "news", "results", "items"):
                value = maybe.get(key)
                if isinstance(value, list):
                    candidates = value
                    break

    items: list[dict[str, Any]] = []
    for entry in candidates[: int(source.get("limit", 4))]:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title") or entry.get("name") or entry.get("headline")
        link = entry.get("url") or entry.get("link") or entry.get("web_url")
        summary = entry.get("summary") or entry.get("abstract") or entry.get("description") or entry.get("teaser")
        published = (
            entry.get("published")
            or entry.get("published_at")
            or entry.get("date")
            or entry.get("updated")
            or entry.get("created")
        )
        item = _normalize_item(
            source=source["name"],
            category=source["category"],
            title=title,
            link=link,
            summary=summary,
            published=published,
        )
        if item:
            items.append(item)
    return items


def _dedupe_and_sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted(items, key=lambda row: (row.get("published_ts") or 0), reverse=True):
        key = f"{item.get('source','').lower()}|{item.get('title','').lower()}|{item.get('url','').lower()}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def fetch_daily_newsletter() -> list[dict[str, Any]]:
    cached = _load_cache()
    if cached is not None:
        return cached

    collected: list[dict[str, Any]] = []
    for source in RSS_SOURCES:
        collected.extend(_parse_rss_source(source))
    for source in JSON_SOURCES:
        collected.extend(_parse_world_bank_source(source))
    for source in HTML_SOURCES:
        collected.extend(_parse_html_source(source))

    collected = _dedupe_and_sort(collected)
    if collected:
        _save_cache(collected)
    return collected
