# Corgi Mention Bot

Get a Telegram message every time someone @-mentions an X (Twitter) account
(default: **@UseCorgi**).

It polls [TwitterAPI.io](https://twitterapi.io) — a third-party X data API that
needs **no official X developer account** and is ~33× cheaper than X's own API —
for new mentions and forwards each one to a Telegram chat.

## How it works

```
TwitterAPI.io  /twitter/user/mentions   ──poll every N sec──▶  corgi_mention_bot.py  ──▶  Telegram bot  ──▶  you
```

- Tracks a high-water-mark tweet id in `state_<username>.json`, so you never get
  the same mention twice.
- On **every run** it delivers all mentions newer than that mark — so any
  mentions that arrived while the bot was down are **backlogged** and sent on
  the next run (nothing is skipped). The first ever run (no saved state) pulls
  the current backlog of recent mentions.
- Only literal `@<handle>` tags are caught (this is what you asked for) — not
  untagged "Corgi" text.

## Setup (one time)

1. **Install dependencies**

   ```bash
   cd telegrambots
   pip install -r requirements.txt
   ```

2. **TwitterAPI.io key** — already set in `.env.local` as `TWITTERAPI_IO_KEY`.
   Add credits in your [TwitterAPI.io dashboard](https://twitterapi.io) (reads
   are ~$0.15 per 1,000 tweets).

3. **Create a Telegram bot**
   - In Telegram, message **@BotFather** → `/newbot` → follow prompts.
   - Copy the token it gives you into `TELEGRAM_BOT_TOKEN` in `.env.local`.

4. **Get your chat id**
   - Open a chat with your new bot and send it any message (e.g. `hi`).
   - Run:

     ```bash
     python corgi_mention_bot.py --get-chat-id
     ```
   - Copy the printed `chat_id` into `TELEGRAM_CHAT_ID` in `.env.local`.
   - (For alerts in a group/channel, add the bot there and use that chat id.)

5. **Test the connection**

   ```bash
   python corgi_mention_bot.py --test
   ```
   You should receive a confirmation message in Telegram.

## Run it

Continuous (keeps running, polls every `POLL_INTERVAL_SECONDS`):

```bash
python corgi_mention_bot.py
```

Single poll (for schedulers):

```bash
python corgi_mention_bot.py --once
```

### Keep it running 24/7

- **Windows Task Scheduler** — create a task that runs
  `python <full-path>\corgi_mention_bot.py --once` every few minutes, **or**
  runs `corgi_mention_bot.py` (loop mode) at logon.
- **A small always-on host** (cheap VPS, Raspberry Pi, etc.) — run the loop mode
  under a process manager so it restarts on reboot.
- **GitHub Actions (free, no server)** — see below.

## Free 24/7 hosting on GitHub Actions

The workflow at `.github/workflows/corgi-mention-bot.yml` runs `--once` on a
schedule (every 5 min) on GitHub's free runners. No server to manage.

How it works:

- Cron triggers a run; it installs deps and runs one poll.
- The high-water-mark `state_*.json` is persisted between runs via the Actions
  **cache** (a fresh per-run key + `restore-keys` prefix, since cache entries
  are immutable). So each run resumes instead of re-baselining.
- `concurrency` prevents two runs from overlapping and racing on the state.

> Note: each run delivers everything newer than the saved high-water mark, so a
> gap between runs (or a paused workflow) is backlogged on the next run. To avoid
> flooding the chat or tripping Telegram's rate limit, a single run delivers at
> most `BACKLOG_LIMIT` mentions (default 100), paced `TELEGRAM_SEND_DELAY_SECONDS`
> apart; any older overflow is skipped and the bot resumes from live time.

### Setup

1. **Push this repo to GitHub** (already remote: `neelmajmudar/GrowthAutomations`).
2. In the repo on GitHub → **Settings → Secrets and variables → Actions →
   Secrets** → add:
   - `TWITTERAPI_IO_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`  ← use your **group** chat id (see below)
   - `OPENAI_API_KEY` *(optional — omit to disable sentiment flagging)*
3. *(Optional)* Under the **Variables** tab, override defaults:
   `MONITOR_USERNAME`, `SENTIMENT_ENABLED`, `SENTIMENT_MODEL`,
   `SENTIMENT_CONFIDENCE_THRESHOLD`, `MAX_PAGES_PER_POLL`.
4. **Actions** tab → enable workflows if prompted → open *Corgi Mention Bot* →
   **Run workflow** to fire the first (baseline) run manually. After that the
   cron takes over.

### Caveats

- GitHub cron has a **5-minute minimum** and runs can be **delayed** under load,
  so alerts aren't real-time — fine for sentiment monitoring.
- Hosting is free, but TwitterAPI.io reads and OpenAI scoring still cost money
  per call (pennies). A group with many viewers costs the same as one viewer —
  it's still a single poller watching a single account.
- Scheduled workflows are auto-disabled after **60 days of repo inactivity**;
  GitHub emails you, and any push (or a manual run) re-enables them.

## Sharing alerts in a group chat

This bot is **one-way**: it only posts alerts, it doesn't read or respond to
messages. To let others see its output, just point it at a group:

1. Add your bot (from @BotFather) to the Telegram group.
2. Post any message in the group, then run locally:

   ```bash
   python corgi_mention_bot.py --get-chat-id
   ```

3. Copy the **group** chat id (negative, e.g. `-1001234567890`) into
   `TELEGRAM_CHAT_ID` (in `.env.local` for local runs, or the GitHub secret for
   hosted runs).
4. Everyone in the group now sees every mention alert.

Members are view-only — they all watch the same `MONITOR_USERNAME`. Making the
bot interactive (per-user `/watch <handle>`, `/pause`, etc.) would require
adding Telegram command handling and per-chat state.

## Configuration (`.env.local`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `TWITTERAPI_IO_KEY` | yes | — | Key from twitterapi.io dashboard |
| `TELEGRAM_BOT_TOKEN` | yes | — | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | yes | — | Where to send alerts (`--get-chat-id`) |
| `MONITOR_USERNAME` | no | `UseCorgi` | Handle to watch (no `@`) |
| `POLL_INTERVAL_SECONDS` | no | `120` | Seconds between polls |
| `MAX_PAGES_PER_POLL` | no | `5` | Cost-safety cap (20 mentions/page) |
| `BACKLOG_LIMIT` | no | `10` | Max mentions delivered per run; older overflow is skipped and the bot jumps to live time (kept small to stay live; `0` = unlimited) |
| `TELEGRAM_SEND_DELAY_SECONDS` | no | `3` | Pause between sends to stay under Telegram's ~20 msg/min group limit |
| `OPENAI_API_KEY` | no | — | Enables negative-sentiment flagging |
| `SENTIMENT_ENABLED` | no | `true` | Set `false` to disable scoring |
| `SENTIMENT_MODEL` | no | `gpt-4o-mini` | OpenAI model for scoring |
| `SENTIMENT_CONFIDENCE_THRESHOLD` | no | `0.7` | Only flag when confidence ≥ this |

## Negative sentiment flagging

When `OPENAI_API_KEY` is set, each new mention is scored by a small LLM
(`gpt-4o-mini` by default). **All mentions still notify you as before** — negative
ones additionally get a **🚨 NEGATIVE MENTION** header with a short reason.

- If scoring fails or the key is missing, alerts still send (fail-open).
- Scores are cached in `state_<username>.json` so restarts do not re-score.
- Test scoring without polling:

  ```bash
  python corgi_mention_bot.py --test-sentiment "Corgi is a scam, avoid them"
  python corgi_mention_bot.py --test-sentiment "loved the matcha at @UseCorgi cafe!"
  ```

Cost: roughly $0.0001–0.0003 per mention scored.

## Cost

You pay TwitterAPI.io per tweet read (~$0.00015 each). Idle polls that return no
new mentions cost nothing. Faster polling (lower `POLL_INTERVAL_SECONDS`) =
fresher alerts but more reads when mention volume is high.

## Official X API variant

`corgi_mention_bot_official.py` is the same bot but uses the **official X API v2**
(`GET /2/users/:id/mentions`, app-only auth via your `x_bearer_token`) instead of
TwitterAPI.io. Same commands (`--test`, `--once`, `--get-chat-id`, `--catchup`)
and the same Telegram config.

- It resolves the handle to a numeric user id once and caches it, then polls
  incrementally with `since_id`. State lives in `state_official_<username>.json`.
- **Billing caveat:** the official API is pay-per-use as of Feb 2026. Your
  account must have credits or every read returns `HTTP 402 CreditsDepleted`
  (your account currently has none). Mention reads bill as third-party post
  reads (~$0.005 each) — far pricier than TwitterAPI.io.

Use the TwitterAPI.io version (`corgi_mention_bot.py`) unless you specifically
need official-API data; use this one once you've loaded X API credits.

## Notes

- `.env.local` and `state_*.json` are git-ignored — never commit your keys.
- To reset the baseline (e.g., re-watch from now), delete the relevant
  `state_<username>.json` / `state_official_<username>.json`.
