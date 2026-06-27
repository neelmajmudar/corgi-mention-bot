"""
Corgi Mention Bot — Telegram notifier for X (Twitter) mentions.

Polls TwitterAPI.io (a third-party X data API — no official X developer
account required) for new posts that @-mention a target account (default:
@UseCorgi) and pushes each new one to a Telegram chat via a bot.

Why polling and not streaming: TwitterAPI.io exposes a clean mentions
endpoint and per-call billing, so a lightweight poller is the cheapest,
simplest, and most reliable option for a personal alert bot.

Usage:
    python corgi_mention_bot.py              # run forever, polling on an interval
    python corgi_mention_bot.py --once       # one poll then exit (for Task Scheduler / cron)
    python corgi_mention_bot.py --test       # send a test Telegram message and exit
    python corgi_mention_bot.py --get-chat-id  # print chat IDs the bot can see, then exit

On a fresh deployment the first run baselines to the newest mention (no initial
dump). After that the bot tracks a high-water-mark tweet id and, on every run,
delivers all mentions newer than it — so any mentions that arrived while the bot
was not running are backlogged and sent on the next run. A single run delivers
at most BACKLOG_LIMIT mentions (most-recent first); any older overflow is skipped
and the bot resumes from live time, so it never floods the chat or rate-limits.

Config is read from telegrambots/.env.local (see README.md):
    TWITTERAPI_IO_KEY     (required) your key from https://twitterapi.io dashboard
    TELEGRAM_BOT_TOKEN    (required) from @BotFather
    TELEGRAM_CHAT_ID      (required) your chat/user/channel id (use --get-chat-id)
    MONITOR_USERNAME      (optional) handle to watch, no @  (default: UseCorgi)
    POLL_INTERVAL_SECONDS (optional) seconds between polls   (default: 120)
    MAX_PAGES_PER_POLL    (optional) cost safety cap          (default: 5)
    BACKLOG_LIMIT         (optional) max mentions per run, then go live (default: 100; 0=unlimited)
    TELEGRAM_SEND_DELAY_SECONDS (optional) pacing between sends (default: 3)
    OPENAI_API_KEY        (optional) for negative-sentiment flagging
    SENTIMENT_ENABLED     (optional) true/false                 (default: true)
    SENTIMENT_MODEL       (optional) OpenAI model              (default: gpt-4o-mini)
    SENTIMENT_CONFIDENCE_THRESHOLD (optional) 0.0-1.0          (default: 0.7)
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

from sentiment import SentimentResult, analyze_mention, sentiment_active

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(_SCRIPT_DIR / ".env.local", override=True)

TWITTERAPI_IO_KEY = os.getenv("TWITTERAPI_IO_KEY") or os.getenv("TWITTERAPI_TOKEN") or ""
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MONITOR_USERNAME = os.getenv("MONITOR_USERNAME", "UseCorgi").lstrip("@")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
MAX_PAGES_PER_POLL = int(os.getenv("MAX_PAGES_PER_POLL", "5"))

# Backlog cap: if a single poll finds more new mentions than this, only the
# most recent BACKLOG_LIMIT are delivered and the high-water mark jumps past the
# older ones — i.e. catch up on at most this many, then continue from live time.
# Kept small so the bot stays effectively live and never floods after a gap.
# Set to 0 to disable the cap (deliver the entire backlog).
BACKLOG_LIMIT = int(os.getenv("BACKLOG_LIMIT", "10"))

# Seconds to wait between consecutive Telegram sends. Telegram throttles bots to
# ~20 messages/minute per group, so we pace backlog delivery to avoid HTTP 429.
TELEGRAM_SEND_DELAY_SECONDS = float(os.getenv("TELEGRAM_SEND_DELAY_SECONDS", "3"))

STATE_FILE = _SCRIPT_DIR / f"state_{MONITOR_USERNAME.lower()}.json"

TWITTERAPI_BASE = "https://api.twitterapi.io"
MENTIONS_ENDPOINT = f"{TWITTERAPI_BASE}/twitter/user/mentions"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

HTTP_TIMEOUT = 30


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# State (high-water mark so we never notify the same tweet twice)
# ──────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log(f"WARN: could not read state file ({e}); starting fresh.")
    return {"last_seen_id": "0", "last_seen_time": 0, "sentiment_cache": {}}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"ERROR: could not write state file: {e}")


def _as_int_id(tweet_id: str) -> int:
    try:
        return int(tweet_id)
    except (TypeError, ValueError):
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# TwitterAPI.io
# ──────────────────────────────────────────────────────────────────────────────

def fetch_new_mentions(last_seen_id: str, since_time: int) -> list[dict]:
    """Return mentions newer than last_seen_id, oldest-first.

    The endpoint returns up to 20 mentions per page, newest first. We page
    until we hit an already-seen id, run out of pages, or hit the safety cap.
    """
    last_id_int = _as_int_id(last_seen_id)
    headers = {"x-api-key": TWITTERAPI_IO_KEY}
    collected: list[dict] = []
    cursor = ""
    pages = 0

    while pages < MAX_PAGES_PER_POLL:
        params = {"userName": MONITOR_USERNAME}
        if since_time:
            # inclusive lower bound; small buffer avoids edge misses, dedupe handles overlap
            params["sinceTime"] = max(0, since_time - 60)
        if cursor:
            params["cursor"] = cursor

        try:
            resp = requests.get(MENTIONS_ENDPOINT, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            log(f"ERROR: mentions request failed: {e}")
            break

        if resp.status_code != 200:
            log(f"ERROR: mentions HTTP {resp.status_code}: {resp.text[:300]}")
            break

        data = resp.json()
        if data.get("status") == "error":
            log(f"ERROR: API returned error: {data.get('message', 'unknown')}")
            break

        tweets = data.get("tweets") or []
        if not tweets:
            break

        reached_seen = False
        for t in tweets:
            if _as_int_id(t.get("id", "0")) > last_id_int:
                collected.append(t)
            else:
                reached_seen = True

        pages += 1

        # Stop once we cross into already-seen territory, or no more pages.
        if reached_seen or not data.get("has_next_page") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

    # oldest-first so Telegram alerts arrive in chronological order
    collected.sort(key=lambda t: _as_int_id(t.get("id", "0")))
    return collected


# ──────────────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(text: str, disable_preview: bool = False, max_retries: int = 5) -> bool:
    """Send a message, honoring Telegram's 429 rate-limit (retry_after) backoff."""
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": disable_preview,
                },
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            log(f"ERROR: Telegram request failed: {e}")
            return False

        if resp.status_code == 429:
            retry_after = 5
            try:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", retry_after))
            except (ValueError, AttributeError):
                pass
            if attempt >= max_retries:
                log(f"ERROR: Telegram still rate-limited after {max_retries} retries; giving up on this message.")
                return False
            wait = min(retry_after, 60) + 1
            log(f"Telegram rate-limited (429); waiting {wait}s then retrying.")
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            log(f"ERROR: Telegram HTTP {resp.status_code}: {resp.text[:300]}")
            return False
        return True

    return False


def format_negative_flag(result: SentimentResult) -> str:
    return (
        f"\U0001F6A8 <b>NEGATIVE MENTION</b> "
        f"(confidence {result.confidence:.0%})\n"
        f"<i>{html.escape(result.reason)}</i>"
    )


def build_telegram_message(t: dict, state: dict) -> str:
    message = format_mention(t)
    if not sentiment_active():
        return message

    result = analyze_mention(t, MONITOR_USERNAME, state)
    if result and result.should_flag():
        log(f"Negative mention flagged ({result.confidence:.0%}): {result.reason}")
        return format_negative_flag(result) + "\n\n" + message
    return message


def format_mention(t: dict) -> str:
    author = t.get("author") or {}
    name = html.escape(author.get("name") or "Unknown")
    handle = html.escape(author.get("userName") or "unknown")
    text = html.escape(t.get("text") or "")
    url = t.get("url") or f"https://x.com/i/web/status/{t.get('id', '')}"
    created = t.get("createdAt") or ""

    likes = t.get("likeCount", 0)
    rts = t.get("retweetCount", 0)
    replies = t.get("replyCount", 0)
    views = t.get("viewCount", 0)

    kind = "Reply mentioning" if t.get("isReply") else "Post mentioning"

    return (
        f"\U0001F514 <b>{kind} @{html.escape(MONITOR_USERNAME)}</b>\n\n"
        f"\U0001F464 <b>{name}</b> (@{handle})\n\n"
        f"{text}\n\n"
        f"\u2764\uFE0F {likes}  \U0001F501 {rts}  \U0001F4AC {replies}  \U0001F441 {views}\n"
        f"\U0001F551 {created}\n"
        f"\U0001F517 {html.escape(url)}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Poll cycle
# ──────────────────────────────────────────────────────────────────────────────

def poll_once(state: dict) -> dict:
    new_tweets = fetch_new_mentions(state.get("last_seen_id", "0"), state.get("last_seen_time", 0))

    if not new_tweets:
        log(f"No new mentions of @{MONITOR_USERNAME}.")
        state["last_seen_time"] = int(time.time())
        save_state(state)
        return state

    found = len(new_tweets)

    # Backlog cap: keep only the most recent BACKLOG_LIMIT (list is oldest-first),
    # and advance the high-water mark past the skipped older ones so we resume
    # from live time instead of endlessly draining a huge backlog.
    if BACKLOG_LIMIT > 0 and found > BACKLOG_LIMIT:
        overflow = found - BACKLOG_LIMIT
        skipped, new_tweets = new_tweets[:overflow], new_tweets[overflow:]
        newest_skipped = max(_as_int_id(t.get("id", "0")) for t in skipped)
        state["last_seen_id"] = str(max(_as_int_id(state.get("last_seen_id", "0")), newest_skipped))
        save_state(state)
        log(f"Found {found} new mention(s); backlog cap is {BACKLOG_LIMIT}, "
            f"skipping {overflow} older one(s) and jumping to live.")

    log(f"Delivering {len(new_tweets)} mention(s) of @{MONITOR_USERNAME}.")
    sent = 0
    for i, t in enumerate(new_tweets):
        if i > 0 and TELEGRAM_SEND_DELAY_SECONDS > 0:
            time.sleep(TELEGRAM_SEND_DELAY_SECONDS)  # pace sends to stay under Telegram's rate limit
        if send_telegram(build_telegram_message(t, state)):
            sent += 1
            state["last_seen_id"] = str(max(_as_int_id(state.get("last_seen_id", "0")), _as_int_id(t.get("id", "0"))))
            save_state(state)  # persist after each send so a crash never re-notifies
        else:
            log("Stopping this cycle after a failed Telegram send; will retry next poll.")
            break

    state["last_seen_time"] = int(time.time())
    save_state(state)
    log(f"Delivered {sent}/{len(new_tweets)} mention(s) to Telegram.")
    return state


# ──────────────────────────────────────────────────────────────────────────────
# Helpers / entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def check_config() -> bool:
    missing = []
    if not TWITTERAPI_IO_KEY:
        missing.append("TWITTERAPI_IO_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log(f"ERROR: missing required config in .env.local: {', '.join(missing)}")
        log("See telegrambots/README.md for setup steps.")
        return False
    if os.getenv("SENTIMENT_ENABLED", "true").lower() not in ("false", "0", "no") and not os.getenv("OPENAI_API_KEY"):
        log("WARN: SENTIMENT_ENABLED but OPENAI_API_KEY is missing - alerts will send without sentiment flagging.")
    return True


def cmd_get_chat_id() -> None:
    """Print chat IDs from recent updates so the user can grab theirs."""
    if not TELEGRAM_BOT_TOKEN:
        log("ERROR: TELEGRAM_BOT_TOKEN is not set.")
        return
    log("Send your bot a message first (e.g. 'hi'), then run this command.")
    try:
        resp = requests.get(f"{TELEGRAM_API}/getUpdates", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"ERROR: getUpdates failed: {e}")
        return

    results = resp.json().get("result", [])
    if not results:
        log("No updates found. Message your bot, then re-run --get-chat-id.")
        return

    seen = set()
    for upd in results:
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is not None and cid not in seen:
            seen.add(cid)
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or chat.get("type")
            log(f"chat_id={cid}  ({label})")


def baseline_state(state: dict) -> dict:
    """Set the high-water mark to the current newest mention without notifying.

    Used on a fresh deployment (no saved state) so the first run does not dump
    the entire recent backlog into the chat. Every later run still polls from the
    saved mark and backlogs any mentions missed while the bot wasn't running.
    """
    log(f"Fresh state — baselining @{MONITOR_USERNAME} to the newest mention (no initial dump).")
    headers = {"x-api-key": TWITTERAPI_IO_KEY}
    try:
        resp = requests.get(MENTIONS_ENDPOINT, headers=headers, params={"userName": MONITOR_USERNAME}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        tweets = resp.json().get("tweets") or []
    except requests.RequestException as e:
        log(f"ERROR: baseline fetch failed: {e}")
        tweets = []

    if tweets:
        newest = max(_as_int_id(t.get("id", "0")) for t in tweets)
        state["last_seen_id"] = str(newest)
    state["last_seen_time"] = int(time.time())
    save_state(state)
    log(f"Baseline set (last_seen_id={state.get('last_seen_id', '0')}). Listening for new mentions from now.")
    return state


def _is_fresh_state(state: dict) -> bool:
    return state.get("last_seen_id", "0") in ("0", "", None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram notifier for X mentions via TwitterAPI.io")
    parser.add_argument("--once", action="store_true", help="poll a single time then exit")
    parser.add_argument("--test", action="store_true", help="send a test Telegram message then exit")
    parser.add_argument("--get-chat-id", action="store_true", help="print chat IDs the bot can see")
    parser.add_argument("--test-sentiment", metavar="TEXT", help="score sample text and print result, then exit")
    args = parser.parse_args()

    if args.test_sentiment:
        from sentiment import score_sentiment
        if not sentiment_active():
            log("ERROR: set OPENAI_API_KEY in .env.local (and SENTIMENT_ENABLED=true) to test.")
            return 1
        result = score_sentiment(args.test_sentiment, MONITOR_USERNAME)
        if not result:
            log("ERROR: sentiment scoring failed.")
            return 1
        log(f"is_negative={result.is_negative} confidence={result.confidence:.0%} reason={result.reason!r}")
        log(f"would_flag={result.should_flag()}")
        return 0

    if args.get_chat_id:
        cmd_get_chat_id()
        return 0

    if not check_config():
        return 1

    if args.test:
        ok = send_telegram(
            f"\u2705 <b>Corgi Mention Bot is connected.</b>\n\nNow watching @{html.escape(MONITOR_USERNAME)} "
            f"for mentions, polling every {POLL_INTERVAL_SECONDS}s."
        )
        log("Test message sent." if ok else "Test message FAILED — check token/chat id.")
        return 0 if ok else 1

    state = load_state()

    if args.once:
        # One-shot / scheduled mode (e.g. GitHub Actions cron): state_*.json is
        # persisted between runs. On a fresh deployment we baseline to the newest
        # mention (no initial dump); every later run polls from the saved
        # high-water mark, backlogging anything missed while the bot was down.
        if _is_fresh_state(state):
            baseline_state(state)
        else:
            log(f"Polling from saved state (last_seen_id={state.get('last_seen_id', '0')}).")
            poll_once(state)
        return 0

    # Long-running loop mode: baseline once on a fresh start, then poll forever.
    if _is_fresh_state(state):
        state = baseline_state(state)

    if sentiment_active():
        log(f"Sentiment flagging enabled (model={os.getenv('SENTIMENT_MODEL', 'gpt-4o-mini')}).")
    log(f"Starting poll loop for @{MONITOR_USERNAME} every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    try:
        while True:
            state = poll_once(state)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
