# Hashtag United MatchDay Discord Bot

Automated bot that fetches match data from FotMob for **Hashtag United** and posts updates to Discord via webhook.

## Features

- Tracks fixtures for team id `1186081` (Hashtag United)
- Posts English updates for:
  - pre-match
  - match live (once)
  - goals during live matches (minute + scorer + scoreline)
  - half-time
  - full-time
  - cancelled matches
  - latest finished match recap (manual mode) with goal scorers + minutes (or explicit N/A if unavailable)
- Includes kickoff time (London), competition + round, and stadium/venue when available
- Prevents duplicate posts with persisted state (`.state/posted_events.json`)
- Supports manual testing modes in GitHub Actions (`dry_run`, `send_test_message`, `send_next_match_now`, `send_latest_finished_match_now`)
- Uses artifact-based state persistence in GitHub Actions (`matchday-state`)

## Setup

1. Create a Discord webhook in the channel where updates should be posted.
2. Set environment variables:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export TEAM_ID="1186081"  # optional
export PREMATCH_WINDOW_MINUTES="120"  # optional
export MATCH_LOOKAHEAD_HOURS="24"  # optional
export SEND_NEXT_MATCH_NOW="false"  # optional
export FAST_WINDOW_BEFORE_MINUTES="60"  # optional
export FAST_WINDOW_AFTER_MINUTES="30"  # optional
export EXPECTED_MATCH_DURATION_MINUTES="120"  # optional
export SLOW_POLL_INTERVAL_MINUTES="30"  # optional
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Run the bot:

```bash
python bot/matchday_bot.py
```

## Testing

### 1) Run automated tests locally

```bash
python -m unittest discover -s tests -v
python -m py_compile bot/matchday_bot.py tests/test_matchday_bot.py
```

### 2) Manual GitHub Actions modes

In **Run workflow**, use these key options:

- **Safe diagnostics**: `dry_run = true`
- **Webhook connectivity test**: `dry_run = false`, `send_test_message = true`
- **Force next match post**: `dry_run = false`, `send_next_match_now = true`
- **Post latest finished recap**: `dry_run = false`, `send_latest_finished_match_now = true`
  - Optional: `force_post = true` to repost same recap
  - Optional: `max_finished_age_hours = 168` to limit how old finished match can be
  - Recap now includes final score, London kickoff, competition/stage, venue and goal list
  - If FotMob does not expose goal incidents, message shows: `⚽ Goals: N/A (source did not provide goal events)`
- **Payload diagnostics**: `debug_fotmob_payload = true` (logs recap source sections and goals parsed count)

## GitHub Actions behavior

Workflow file: `.github/workflows/fotmob-discord.yml`

- Triggered every 5 minutes (`*/5 * * * *`)
- Bot applies adaptive cadence:
  - every 5 minutes from 60 minutes before kickoff to 30 minutes after expected full-time
  - every 30 minutes outside that window
- State is restored/uploaded via artifact `matchday-state` so dedupe survives ephemeral runners

Repository secret required:

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
- `send_next_match_now`
- `send_latest_finished_match_now`
- `force_post`
- `max_finished_age_hours`
- `debug_fotmob_payload`

## Notes

FotMob does not provide an official public API for this setup. The bot uses open JSON endpoints that may change over time.
