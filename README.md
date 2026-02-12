# Hashtag United MatchDay Discord Bot

Automated bot that fetches match data from FotMob for **Hashtag United** and posts updates to a Discord channel via webhook.

## Features

- Tracks fixtures for team id `1186081` (Hashtag United)
- Sends notifications for:
  - upcoming match (pre-match)
  - match live
  - half-time
  - full-time
  - cancelled matches
- Prevents duplicate posts using a local state file
- Can run locally or on a schedule via GitHub Actions
- Discord messages are in English
- Kickoff time is shown in London time (`Europe/London`)
- Competition + round info is included in Discord posts
- Stadium/venue name is included when available

## Setup

1. Create a Discord webhook in the channel where updates should be posted.
2. Set environment variables:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export TEAM_ID="1186081"  # optional
export PREMATCH_WINDOW_MINUTES="120"  # optional
export MATCH_LOOKAHEAD_HOURS="24"  # optional
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Run the bot:

```bash
python bot/matchday_bot.py
```

## How to test the bot

### 1) Run automated tests locally

```bash
python -m unittest discover -s tests -v
python -m py_compile bot/matchday_bot.py tests/test_matchday_bot.py
```

If both commands pass, core event logic and syntax are valid.

### 2) Quick functional test against Discord

1. Create a test channel in Discord and create a webhook for that channel.
2. Set the webhook variable:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

3. Run the bot:

```bash
python bot/matchday_bot.py
```

4. Check that a message appears in your test channel (if a relevant match exists in the current time window).
5. Run the command again immediately. You should normally not get duplicate posts, because posted events are stored in `.state/posted_events.json`.

### 3) Test via GitHub Actions (including direct Discord post)

When starting the workflow manually (**Run workflow**), you can use two test modes:

**A. Safe test (no post):**
- `dry_run = true`

**B. Direct test post to Discord:**
- `dry_run = false`
- `send_test_message = true`
- optionally customize `test_message`

With `send_test_message = true`, the workflow posts one direct message to your webhook, so you can immediately verify that GitHub Actions can post in the channel.

Tip: Discord webhooks often return HTTP `204 No Content` on success. The bot handles this correctly.

If you are unsure about data access, set `debug_fotmob_payload = true` in a manual workflow run. This logs sample data (for example match id, team names, status, and score) directly from FotMob before the bot runs.


### 4) Verify FotMob data is actually being parsed

Use a manual workflow run with:

- `dry_run = true`
- `debug_fotmob_payload = true`
- `send_test_message = false`

Then inspect logs from **Debug FotMob payload**:

- `fixtures_count` should be greater than `0`
- `parsed_match_objects` should be greater than `0`
- `parsed_matches_with_utcTime` should be greater than `0`
- `parsed_matches_in_bot_window` should ideally be greater than `0`
- `match_lookahead_hours` controls this window (default: `24`)
- `sample_match` should include fields like `home`, `away`, `utcTime`

Note: FotMob fixture arrays are not always ordered by "closest kickoff". The debug step now selects the match nearest to current time (preferably within the same time window the bot uses), so you do not get misleading old sample matches by default.

If `fixtures_count > 0` but `parsed_matches_with_utcTime = 0`, FotMob changed shape and you should use the printed raw fixture sample to update parsing.

### 5) Verify that data reaches Discord

Recommended two-step check:

1. **Webhook connectivity test**
   - Run workflow with `dry_run = false` and `send_test_message = true`
   - You should see `Posted test message to Discord.` in logs, and a test message in the Discord channel.

2. **Real FotMob event test**
   - Run workflow with `dry_run = false`, `send_test_message = false`, `debug_fotmob_payload = true`
   - If a match event is within the bot's time window, logs should show `Posted: <event_id>` and message appears in Discord.
   - If logs show `No new events to post.`, the bot is healthy but there were no new qualifying events at that run time.

## GitHub Actions (automated operation)

The workflow in `.github/workflows/fotmob-discord.yml` runs every 10 minutes.

Add repository secret:

- `DISCORD_WEBHOOK_URL`

Optional repository variables:

- `TEAM_ID`
- `PREMATCH_WINDOW_MINUTES`
- `MATCH_LOOKAHEAD_HOURS`

Manual `workflow_dispatch` inputs:

- `team_id`
- `prematch_window_minutes`
- `match_lookahead_hours`
- `dry_run`
- `send_test_message`
- `test_message`
- `debug_fotmob_payload`

## Notes

FotMob does not provide an official public API for this setup. The bot uses an open JSON endpoint that may change over time.

For better fixture compatibility, the bot calls the team endpoint with `timezone=Europe/London` and `ccode3=GBR`, and parses multiple possible fixture shapes.
The bot filters matches to a dynamic window from 4 hours in the past to `MATCH_LOOKAHEAD_HOURS` in the future (default `24`).
