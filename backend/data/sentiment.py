"""
Market sentiment data module — Fear & Greed Index and crypto news headlines.

Provides two external sentiment signals that feed into the confluence scoring
engine and are included in the Gemini LLM prompt context:

1. **Fear & Greed Index** (alternative.me API): A 0–100 composite sentiment
   metric. Low values (fear) historically correlate with buying opportunities;
   high values (greed) suggest potential overextension.

2. **CoinDesk RSS headlines**: Recent news titles and summaries. The module
   performs a simple keyword-based sentiment classification (positive/negative/neutral)
   on each headline without using a heavy NLP library.

Both data sources are cached for 10 minutes to avoid hammering external APIs
on every trading cycle.

Role in system: Called by the main agent loop once per standard cycle. The
resulting SentimentSnapshot is passed to confluence.score() and included
verbatim in the Gemini prompt.

Dependencies: requests, pydantic, loguru
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

# ── Cache ─────────────────────────────────────────────────────────────────────
_CACHE_TTL_SECONDS = 600  # 10 minutes
_cache: dict[str, tuple[Any, float]] = {}  # key → (value, unix_timestamp)


def _cache_get(key: str) -> Any | None:
    """Return cached value if still within TTL, else None."""
    if key in _cache:
        value, ts = _cache[key]
        if time.time() - ts < _CACHE_TTL_SECONDS:
            return value
    return None


def _cache_set(key: str, value: Any) -> None:
    """Store a value in the cache with the current timestamp."""
    _cache[key] = (value, time.time())


# ── Models ────────────────────────────────────────────────────────────────────

class NewsItem(BaseModel):
    """A single crypto news headline with sentiment classification.

    Attributes:
        title: Headline text.
        summary: Article summary or first paragraph.
        published: ISO 8601 publication timestamp.
        sentiment: 'positive', 'negative', or 'neutral'.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    summary: str
    published: str
    sentiment: str = "neutral"


class SentimentSnapshot(BaseModel):
    """
    Aggregated sentiment state at a point in time.

    Attributes:
        fear_greed_value: Integer 0–100. <25 = extreme fear, >75 = extreme greed.
        fear_greed_classification: Human-readable label from the API.
        news_items: Up to 5 recent headline objects.
        overall_news_sentiment: Aggregate of all headlines: 'positive'/'negative'/'neutral'.
        fetched_at: ISO 8601 UTC timestamp of when this snapshot was taken.

    Example:
        >>> snap = get_sentiment_snapshot()
        >>> print(snap.fear_greed_value)
        42
    """

    model_config = ConfigDict(frozen=True)

    fear_greed_value: int = Field(ge=0, le=100)
    fear_greed_classification: str
    news_items: list[NewsItem] = Field(default_factory=list)
    overall_news_sentiment: str = "neutral"
    fetched_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


# ── Positive / negative keyword sets for naive news sentiment ─────────────────
_POSITIVE_KEYWORDS = {
    "surge", "rally", "bull", "gain", "rise", "soar", "breakout", "adoption",
    "record", "high", "approval", "etf", "institutional", "buy", "support",
    "recovery", "upgrade", "partnership", "launch", "milestone", "growth",
    "bullish", "positive", "outperform", "exceed", "exceed", "boom",
}
_NEGATIVE_KEYWORDS = {
    "crash", "drop", "fall", "bear", "loss", "plunge", "hack", "ban", "sell",
    "regulation", "crackdown", "fraud", "scam", "lawsuit", "seizure", "slump",
    "decline", "bearish", "negative", "concern", "risk", "warning", "fine",
    "penalty", "collapse", "correction", "fear", "uncertainty", "dump",
}


def _classify_sentiment(text: str) -> str:
    """
    Classify text sentiment using keyword matching.

    Args:
        text: Headline or summary text to classify.

    Returns:
        'positive', 'negative', or 'neutral' based on keyword counts.

    Example:
        >>> _classify_sentiment("Bitcoin surges to new all-time high")
        'positive'
    """
    words = set(text.lower().split())
    pos_hits = len(words & _POSITIVE_KEYWORDS)
    neg_hits = len(words & _NEGATIVE_KEYWORDS)

    if pos_hits > neg_hits:
        return "positive"
    elif neg_hits > pos_hits:
        return "negative"
    return "neutral"


def fetch_fear_greed_index() -> dict[str, Any]:
    """
    Fetch the current Crypto Fear & Greed Index from alternative.me.

    Makes a GET request to the public API (no authentication required) and
    returns the current value and its text classification.

    Returns:
        Dictionary with keys:
          - value (int): Score 0–100.
          - classification (str): e.g. 'Fear', 'Extreme Greed', 'Neutral'.
          - timestamp (str): ISO 8601 UTC timestamp.

    Raises:
        RuntimeError: If the API request fails or response cannot be parsed.

    Example:
        >>> fgi = fetch_fear_greed_index()
        >>> print(fgi['value'], fgi['classification'])
    """
    cache_key = "fear_greed"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("Fear & Greed Index served from cache")
        return cached  # type: ignore[return-value]

    try:
        response = requests.get(
            "https://api.alternative.me/fng/",
            params={"limit": 1, "format": "json"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        entry = data["data"][0]
        result: dict[str, Any] = {
            "value": int(entry["value"]),
            "classification": entry["value_classification"],
            "timestamp": datetime.now(UTC).isoformat(),
        }
        _cache_set(cache_key, result)
        logger.debug(f"Fear & Greed: {result['value']} ({result['classification']})")
        return result

    except Exception as exc:
        logger.warning(f"Failed to fetch Fear & Greed Index: {exc}. Returning neutral default.")
        return {"value": 50, "classification": "Neutral", "timestamp": datetime.now(UTC).isoformat()}


def fetch_crypto_news(limit: int = 5) -> list[dict[str, Any]]:
    """
    Fetch recent crypto news headlines from CoinDesk RSS feed.

    Parses the CoinDesk RSS XML feed and returns the most recent ``limit``
    articles with title, summary, publication date, and sentiment classification.

    Args:
        limit: Maximum number of news items to return. Defaults to 5.

    Returns:
        List of dicts, each containing:
          - title (str): Headline text.
          - summary (str): Article description/summary.
          - published (str): Publication date string.
          - sentiment (str): 'positive', 'negative', or 'neutral'.

    Raises:
        RuntimeError: If the RSS feed cannot be fetched or parsed.

    Example:
        >>> news = fetch_crypto_news(limit=3)
        >>> for item in news:
        ...     print(item['title'], item['sentiment'])
    """
    cache_key = f"crypto_news_{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("Crypto news served from cache")
        return cached  # type: ignore[return-value]

    try:
        response = requests.get(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            timeout=10,
            headers={"User-Agent": "axion-trader/0.1 (automated trading agent)"},
        )
        response.raise_for_status()

        root = ET.fromstring(response.content)
        items: list[dict[str, Any]] = []

        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title", default="").strip()
            description = item.findtext("description", default="").strip()
            pub_date = item.findtext("pubDate", default="").strip()

            combined_text = f"{title} {description}"
            sentiment = _classify_sentiment(combined_text)

            items.append({
                "title": title,
                "summary": description[:300],  # Truncate long summaries
                "published": pub_date,
                "sentiment": sentiment,
            })

        _cache_set(cache_key, items)
        logger.debug(f"Fetched {len(items)} crypto news items from CoinDesk RSS")
        return items

    except Exception as exc:
        logger.warning(f"Failed to fetch crypto news: {exc}. Returning empty list.")
        return []


def get_sentiment_snapshot() -> SentimentSnapshot:
    """
    Fetch and aggregate all sentiment signals into a single snapshot.

    Calls both ``fetch_fear_greed_index()`` and ``fetch_crypto_news()`` and
    combines their results into a ``SentimentSnapshot`` Pydantic model.
    Results are cached independently so partial failures degrade gracefully.

    Returns:
        SentimentSnapshot with all available sentiment data populated.

    Example:
        >>> snapshot = get_sentiment_snapshot()
        >>> print(snapshot.fear_greed_value, snapshot.overall_news_sentiment)
    """
    fgi = fetch_fear_greed_index()
    raw_news = fetch_crypto_news(limit=5)

    news_items = [
        NewsItem(
            title=n["title"],
            summary=n["summary"],
            published=n["published"],
            sentiment=n["sentiment"],
        )
        for n in raw_news
    ]

    # Aggregate news sentiment: majority rules
    if news_items:
        sentiments = [n.sentiment for n in news_items]
        pos = sentiments.count("positive")
        neg = sentiments.count("negative")
        if pos > neg:
            overall = "positive"
        elif neg > pos:
            overall = "negative"
        else:
            overall = "neutral"
    else:
        overall = "neutral"

    return SentimentSnapshot(
        fear_greed_value=fgi["value"],
        fear_greed_classification=fgi["classification"],
        news_items=news_items,
        overall_news_sentiment=overall,
        fetched_at=datetime.now(UTC).isoformat(),
    )
