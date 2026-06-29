"""LLM-based sentiment scoring for @-mention alerts."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from openai import OpenAI

SYSTEM_PROMPT = """You classify whether a tweet mentioning the brand @{brand} is NEGATIVE toward the brand.

{brand} is an AI insurance / fintech company with a physical cafe in San Francisco that
hosts hackathons and builder events.

This is startup / builder Twitter. Builder slang is POSITIVE, not hostile: words like
"hacking", "hacking at night", "shipping", "building", "cooking", "cracked", "grinding",
"locked in", "insane", "sick", "goated" describe people coding and building hard — they
are praise, NOT cyber-attacks, wrongdoing, or complaints. In particular, "hacking" /
"hackathon" describes building software and is POSITIVE by default. Treat "hacking" as
negative ONLY when it clearly describes a security breach, a hacked/stolen account, stolen
funds, or an exploit affecting the brand or its users.

Negative = complaints, anger, scam/fraud accusations, hate, threats, public shaming,
"terrible service", sarcastic attacks directed at the brand, warnings to avoid them, or a
genuine report that the brand was breached/hacked/exploited.

NOT negative = neutral mentions, genuine questions, praise, event invites, partner tags,
positive hype ("this cafe is insane"), builder / hackathon culture ("hacking all night
@{brand}"), jokes without hostility, logistics ("where is the bus").

When the tweet is ambiguous and there is no clear hostility toward the brand, default to
is_negative=false. Do not flag a tweet as negative just because it contains a single
charged-sounding keyword; judge the actual intent toward the brand.

Return JSON only with this exact shape:
{{"is_negative": true or false, "confidence": 0.0 to 1.0, "reason": "five words max"}}"""


@dataclass
class SentimentResult:
    is_negative: bool
    confidence: float
    reason: str

    def should_flag(self) -> bool:
        return self.is_negative and self.confidence >= _confidence_threshold()


def _sentiment_enabled() -> bool:
    return os.getenv("SENTIMENT_ENABLED", "true").lower() not in ("false", "0", "no")


def _confidence_threshold() -> float:
    return float(os.getenv("SENTIMENT_CONFIDENCE_THRESHOLD", "0.7"))


def sentiment_active() -> bool:
    return _sentiment_enabled() and bool(os.getenv("OPENAI_API_KEY"))


def score_sentiment(tweet_text: str, brand_username: str) -> SentimentResult | None:
    """Score tweet text. Returns None on API/parse failure (fail-open)."""
    if not tweet_text.strip():
        return SentimentResult(is_negative=False, confidence=1.0, reason="empty tweet")

    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("SENTIMENT_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)
    brand = brand_username.lstrip("@")
    system = SYSTEM_PROMPT.format(brand=brand)

    try:
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": tweet_text[:2000]},
            ],
            temperature=0,
            max_tokens=80,
        )
        raw = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return None

    return SentimentResult(
        is_negative=bool(raw.get("is_negative", False)),
        confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.0)))),
        reason=str(raw.get("reason", ""))[:80],
    )


def get_cached_sentiment(tweet_id: str, state: dict) -> SentimentResult | None:
    entry = (state.get("sentiment_cache") or {}).get(str(tweet_id))
    if not entry:
        return None
    return SentimentResult(**entry)


def cache_sentiment(tweet_id: str, result: SentimentResult, state: dict) -> None:
    cache = state.setdefault("sentiment_cache", {})
    cache[str(tweet_id)] = asdict(result)
    if len(cache) > 500:
        for old_id in sorted(cache.keys(), key=int)[:-500]:
            del cache[old_id]


def analyze_mention(tweet: dict, brand_username: str, state: dict) -> SentimentResult | None:
    """Return cached or freshly scored sentiment. None if scoring disabled or failed."""
    if not sentiment_active():
        return None

    tweet_id = str(tweet.get("id", ""))
    cached = get_cached_sentiment(tweet_id, state)
    if cached:
        return cached

    result = score_sentiment(tweet.get("text") or "", brand_username)
    if result:
        cache_sentiment(tweet_id, result, state)
    return result
