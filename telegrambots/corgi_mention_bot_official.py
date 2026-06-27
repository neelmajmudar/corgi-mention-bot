"""
Corgi Mention Bot (Official X API) — Telegram notifier for X mentions.

Same behavior as corgi_mention_bot.py, but uses the OFFICIAL X (Twitter)
API v2 instead of the third-party TwitterAPI.io service. It polls
GET /2/users/:id/mentions (app-only auth with your bearer token) and pushes
each new mention to a Telegram chat.

IMPORTANT — billing:
    The official X API is pay-per-use as of Feb 2026. Your enrolled account
    must have a positive credit balance or every read returns HTTP 402
    ("CreditsDepleted"). Reads of mentions for another account are billed as
    third-party post reads (~$0.005 each). Load credits in the X developer
    portal before relying on this script.

Usage:
    python corgi_mention_bot_official.py              # poll forever
    python corgi_mention_bot_official.py --once       # one poll then exit
    python corgi_mention_bot_official.py --test       # send a test Telegram message
    python corgi_mention_bot_official.py --get-chat-id  # print chat IDs, then exit
    python corgi_mention_bot_official.py --catchup 5  # on first run, send latest 5

Config is read from telegrambots/.env.local:
    x_bearer_token        (required) official X API v2 app-only bearer token
    TELEGRAM_BOT_TOKEN    (required) from @BotFather
    TELEGRAM_CHAT_ID      (required) your chat/user/channel id (use --get-chat-id)
    MONITOR_USERNAME      (optional) handle to watch, no @  (default: UseCorgi)
    POLL_INTERVAL_SECONDS (optional) seconds between polls   (default: 120)
    MAX_PAGES_PER_POLL    (optional) cost safety cap          (default: 5)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(_SCRIPT_DIR / ".env.local", override=True)

X_BEARER_TOKEN = os.getenv("x_bearer_token") or os.getenv("X_BEARER_TOKEN") or ""
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MONITOR_USERNAME = os.getenv("MONITOR_USERNAME", "UseCorgi").lstrip("@")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
MAX_PAGES_PER_POLL = int(os.getenv("MAX_PAGES_PER_POLL", "5"))

STATE_FILE = _SCRIPT_DIR / f"state_official_{MONITOR_USERNAME.lower()}.json"

X_API_BASE = "https://api.x.com/2"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

HTTP_TIMEOUT = 30

TWEET_FIELDS = "created_at,public_metrics,author_id,lang,in_reply_to_user_id"
USER_FIELDS = "username,name,verified"


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {X_BEARER_TOKEN}"}


# ──────────────────────────────────────────────────────────────────────────────
# State (caches resolved user id + since_id high-water mark)
# ──────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log(f"WARN: could not read state file ({e}); starting fresh.")
    return {"user_id": "", "since_id": "0"}


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


def _handle_x_error(resp: requests.Response) -> None:
    """Log X API errors with the common ones spelled out."""
    if resp.status_code == 402:
        log("ERROR: HTTP 402 CreditsDepleted — your X API account has no credits. "
            "Load credits in the developer portal before this script can read data.")
    elif resp.status_code == 401:
        log("ERROR: HTTP 401 Unauthorized — check x_bearer_token in .env.local.")
    elif resp.status_code == 429:
        log("ERROR: HTTP 429 Rate limited — backing off until next poll.")
    else:
        log(f"ERROR: X API HTTP {resp.status_code}: {resp.text[:300]}")


# ──────────────────────────────────────────────────────────────────────────────
# X API v2
# ──────────────────────────────────────────────────────────────────────────────

def resolve_user_id(username: str) -> str:
    """Look up the numeric user id for a handle (cached in state)."""
    try:
        resp = requests.get(
            f"{X_API_BASE}/users/by/username/{username}",
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        log(f"ERROR: user lookup failed: {e}")
        return ""

    if resp.status_code != 200:
        _handle_x_error(resp)
        return ""

    return (resp.json().get("data") or {}).get("id", "")


def fetch_new_mentions(user_id: str, since_id: str) -> tuple[list[dict], dict]:
    """Return (tweets oldest-first, user_map) for mentions newer than since_id.

    Uses since_id for precise incremental polling. Pages via next_token up to
    the safety cap. Returns a map of author_id -> {username, name}.
    """
    collected: list[dict] = []
    user_map: dict[str, dict] = {}
    pagination_token = ""
    pages = 0

    while pages < MAX_PAGES_PER_POLL:
        params = {
            "max_results": 100,
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": USER_FIELDS,
        }
        if _as_int_id(since_id) > 0:
            params["since_id"] = since_id
        if pagination_token:
            params["pagination_token"] = pagination_token

        try:
            resp = requests.get(
                f"{X_API_BASE}/users/{user_id}/mentions",
                headers=_auth_headers(),
                params=params,
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            log(f"ERROR: mentions request failed: {e}")
            break

        if resp.status_code != 200:
            _handle_x_error(resp)
            break

        payload = resp.json()
        for u in (payload.get("includes") or {}).get("users", []):
            user_map[u["id"]] = {"username": u.get("username", ""), "name": u.get("name", "")}

        data = payload.get("data") or []
        collected.extend(data)

        meta = payload.get("meta") or {}
        pages += 1
        pagination_token = meta.get("next_token", "")
        if not pagination_token:
            break

    collected.sort(key=lambda t: _as_int_id(t.get("id", "0")))
    return collected, user_map


# ──────────────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(text: str, disable_preview: bool = False) -> bool:
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

    if resp.status_code != 200:
        log(f"ERROR: Telegram HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    return True


def _pretty_time(iso: str) -> str:
    if not iso:
        return ""
    return iso.replace("T", " ").replace(".000Z", " UTC").replace("Z", " UTC")


def format_mention(t: dict, user_map: dict) -> str:
    author = user_map.get(t.get("author_id", ""), {})
    name = html.escape(author.get("name") or "Unknown")
    handle = html.escape(author.get("username") or "unknown")
    text = html.escape(t.get("text") or "")
    tweet_id = t.get("id", "")
    url = f"https://x.com/{handle}/status/{tweet_id}" if handle != "unknown" else f"https://x.com/i/web/status/{tweet_id}"
    created = _pretty_time(t.get("created_at") or "")

    m = t.get("public_metrics") or {}
    likes = m.get("like_count", 0)
    rts = m.get("retweet_count", 0)
    replies = m.get("reply_count", 0)
    views = m.get("impression_count", 0)

    kind = "Reply mentioning" if t.get("in_reply_to_user_id") else "Post mentioning"

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
    user_id = state.get("user_id", "")
    if not user_id:
        log("ERROR: no cached user_id; cannot poll.")
        return state

    new_tweets, user_map = fetch_new_mentions(user_id, state.get("since_id", "0"))

    if not new_tweets:
        log(f"No new mentions of @{MONITOR_USERNAME}.")
        return state

    log(f"Found {len(new_tweets)} new mention(s) of @{MONITOR_USERNAME}.")
    sent = 0
    for t in new_tweets:
        if send_telegram(format_mention(t, user_map)):
            sent += 1
            state["since_id"] = str(max(_as_int_id(state.get("since_id", "0")), _as_int_id(t.get("id", "0"))))
            save_state(state)  # persist after each send so a crash never re-notifies
        else:
            log("Stopping this cycle after a failed Telegram send; will retry next poll.")
            break

    log(f"Delivered {sent}/{len(new_tweets)} mention(s) to Telegram.")
    return state


def establish_baseline(state: dict, catchup: int) -> dict:
    """First run: resolve user id and record the newest mention as baseline."""
    log(f"First run - resolving @{MONITOR_USERNAME} and establishing baseline.")
    user_id = resolve_user_id(MONITOR_USERNAME)
    if not user_id:
        log("ERROR: could not resolve user id (check credits/token). Aborting baseline.")
        return state
    state["user_id"] = user_id
    log(f"Resolved @{MONITOR_USERNAME} -> user_id={user_id}")

    tweets, user_map = fetch_new_mentions(user_id, "0")
    if tweets:
        newest = max(_as_int_id(t.get("id", "0")) for t in tweets)
        state["since_id"] = str(newest)
        if catchup > 0:
            backfill = tweets[-catchup:]
            log(f"Backfilling latest {len(backfill)} mention(s).")
            for t in backfill:
                send_telegram(format_mention(t, user_map))

    save_state(state)
    log(f"Baseline set (since_id={state.get('since_id', '0')}). Future mentions will be notified.")
    return state


# ──────────────────────────────────────────────────────────────────────────────
# Helpers / entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def check_config() -> bool:
    missing = []
    if not X_BEARER_TOKEN:
        missing.append("x_bearer_token")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log(f"ERROR: missing required config in .env.local: {', '.join(missing)}")
        log("See telegrambots/README.md for setup steps.")
        return False
    return True


def cmd_get_chat_id() -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram notifier for X mentions via the official X API v2")
    parser.add_argument("--once", action="store_true", help="poll a single time then exit")
    parser.add_argument("--test", action="store_true", help="send a test Telegram message then exit")
    parser.add_argument("--get-chat-id", action="store_true", help="print chat IDs the bot can see")
    parser.add_argument("--catchup", type=int, default=0, help="on first run, also send the latest N mentions")
    args = parser.parse_args()

    if args.get_chat_id:
        cmd_get_chat_id()
        return 0

    if not check_config():
        return 1

    if args.test:
        ok = send_telegram(
            f"\u2705 <b>Corgi Mention Bot (official X API) is connected.</b>\n\nNow watching "
            f"@{html.escape(MONITOR_USERNAME)} for mentions, polling every {POLL_INTERVAL_SECONDS}s."
        )
        log("Test message sent." if ok else "Test message FAILED — check token/chat id.")
        return 0 if ok else 1

    state = load_state()
    if not state.get("user_id") or _as_int_id(state.get("since_id", "0")) == 0:
        state = establish_baseline(state, args.catchup)
        if not state.get("user_id"):
            return 1
        if args.once:
            return 0

    if args.once:
        poll_once(state)
        return 0

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
