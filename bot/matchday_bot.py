#!/usr/bin/env python3
"""Fetch FotMob match updates for Hashtag United and post to Discord webhook."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# FotMob team endpoint used to retrieve fixtures and match metadata.
FOTMOB_TEAM_FIXTURES_URL = "https://www.fotmob.com/api/teams"
# Local file used to persist already-posted event IDs.
STATE_FILE = Path(".state/posted_events.json")
# All kickoff times are rendered in London time for consistency.
LONDON_TZ = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class MatchEvent:
    """Represents one Discord post candidate."""

    event_id: str
    message: str


def get_env(name: str, default: str | None = None) -> str:
    """Read environment variable or raise if a required value is missing."""
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_as_bool(name: str, default: bool = False) -> bool:
    """Parse an environment variable into a boolean."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_state(path: Path = STATE_FILE) -> set[str]:
    """Load previously posted event IDs from disk."""
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data)


def save_state(event_ids: set[str], path: Path = STATE_FILE) -> None:
    """Persist posted event IDs to disk to prevent duplicate notifications."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(event_ids), indent=2), encoding="utf-8")


def _request_json(url: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Perform a GET/POST HTTP request and return decoded JSON.

    Returns an empty dict for empty bodies (e.g. Discord webhook 204 responses).
    """
    full_url = f"{url}?{urlencode(params)}" if params else url
    payload = None if body is None else json.dumps(body).encode("utf-8")

    req = Request(
        full_url,
        data=payload,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )

    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read()
            if not raw:
                return {}

            decoded = raw.decode("utf-8").strip()
            if not decoded:
                return {}

            return json.loads(decoded)
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"HTTP request failed: {exc}") from exc


def fetch_team_fixtures(team_id: int) -> dict[str, Any]:
    """Fetch team payload from FotMob using parameters that improve fixture availability."""
    return _request_json(
        FOTMOB_TEAM_FIXTURES_URL,
        params={
            "id": team_id,
            "timezone": "Europe/London",
            "ccode3": "GBR",
        },
    )


def _pick_match_obj(item: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a match object from different fixture shapes returned by FotMob."""
    if "match" in item and isinstance(item["match"], dict):
        return item["match"]
    if "fixture" in item and isinstance(item["fixture"], dict):
        return item["fixture"]
    if "status" in item and isinstance(item["status"], dict):
        return item
    return None


def parse_match_utc(match: dict[str, Any]) -> datetime:
    """Parse match UTC timestamp from FotMob status field."""
    status = match.get("status") or {}
    utc_time = status.get("utcTime")
    if not utc_time:
        raise KeyError("Missing status.utcTime in match payload")
    return datetime.fromisoformat(str(utc_time).replace("Z", "+00:00"))


def team_display_name(match: dict[str, Any], team_id: int) -> tuple[str, str]:
    """Return (tracked team, opponent) names based on home/away IDs."""
    home = match.get("home") or {}
    away = match.get("away") or {}
    home_id = home.get("id")

    if home_id is not None and int(home_id) == team_id:
        return home.get("name", "Hashtag United"), away.get("name", "Unknown opponent")

    away_id = away.get("id")
    if away_id is not None and int(away_id) == team_id:
        return away.get("name", "Hashtag United"), home.get("name", "Unknown opponent")

    return home.get("name", "Hashtag United"), away.get("name", "Unknown opponent")


def match_score(match: dict[str, Any]) -> str:
    """Build a printable score string from status.scoreStr or home/away numeric values."""
    status = match.get("status") or {}
    score_str = status.get("scoreStr")
    if score_str:
        return str(score_str)

    home_score = match.get("home", {}).get("score")
    away_score = match.get("away", {}).get("score")
    if home_score is None or away_score is None:
        return "-"
    return f"{home_score}-{away_score}"


def match_round(match: dict[str, Any]) -> str:
    """Extract best-effort round/stage label from a FotMob match payload."""
    candidates = [
        match.get("roundName"),
        match.get("round"),
        (match.get("tournament") or {}).get("roundName"),
        (match.get("tournament") or {}).get("round"),
        (match.get("series") or {}).get("name"),
    ]
    for value in candidates:
        if value:
            return str(value)
    return ""


def match_stadium(match: dict[str, Any]) -> str:
    """Extract best-effort stadium/venue name from a FotMob match payload."""
    candidates = [
        (match.get("venue") or {}).get("name"),
        (match.get("stadium") or {}).get("name"),
        (match.get("ground") or {}).get("name"),
        (match.get("status") or {}).get("venueName"),
        match.get("venueName"),
    ]
    for value in candidates:
        if value:
            return str(value)
    return ""


def build_competition_line(match: dict[str, Any]) -> str:
    """Build 'competition + round' text used in Discord messages."""
    tournament = (match.get("tournament") or {}).get("name") or "Unknown competition"
    round_text = match_round(match)
    return f"🏆 {tournament} {round_text}".strip()


def build_events(
    fixtures: dict[str, Any],
    team_id: int,
    prematch_window_minutes: int,
    match_lookahead_hours: int = 24,
) -> list[MatchEvent]:
    """Convert FotMob payload into Discord-ready match events."""
    now = datetime.now(timezone.utc)
    # Only inspect recent/live/near-future matches to avoid noisy history/far-future fixtures.
    lower = now - timedelta(hours=4)
    upper = now + timedelta(hours=match_lookahead_hours)
    prematch_threshold = now + timedelta(minutes=prematch_window_minutes)

    events: list[MatchEvent] = []
    fixture_items = fixtures.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])

    for item in fixture_items:
        match = _pick_match_obj(item)
        if not match:
            continue

        status = match.get("status") or {}
        # Skip entries that do not include timing/status details.
        if not status.get("utcTime"):
            continue

        try:
            match_time = parse_match_utc(match)
        except KeyError:
            continue

        if not (lower <= match_time <= upper):
            continue

        hashtag_name, opponent_name = team_display_name(match, team_id)
        started = bool(status.get("started"))
        finished = bool(status.get("finished"))
        cancelled = bool(status.get("cancelled"))
        reason = status.get("reason", {})
        reason_code = reason.get("short") or reason.get("long") or ""
        # Fallback to other IDs if nested match ID is missing.
        match_id = str(match.get("id") or item.get("id") or item.get("matchId") or "unknown")
        score = match_score(match)
        competition_line = build_competition_line(match)
        stadium = match_stadium(match)
        stadium_line = f"🏟️ Stadium: {stadium}" if stadium else ""

        if cancelled:
            lines = [
                f"❌ **{hashtag_name} vs {opponent_name}** is cancelled.",
                competition_line,
            ]
            if stadium_line:
                lines.append(stadium_line)
            events.append(MatchEvent(f"{match_id}:cancelled", "\n".join(lines)))
            continue

        if not started and match_time <= prematch_threshold:
            kickoff_london = match_time.astimezone(LONDON_TZ).strftime("%d-%m-%Y %H:%M")
            lines = [
                f"📣 **Match soon:** {hashtag_name} vs {opponent_name}",
                f"🕒 Kickoff (London): {kickoff_london}",
                competition_line,
            ]
            if stadium_line:
                lines.append(stadium_line)
            events.append(MatchEvent(f"{match_id}:prematch", "\n".join(lines)))
            continue

        if started and not finished:
            if str(reason_code).upper() == "HT":
                lines = [
                    f"⏸️ **Half-time:** {hashtag_name} vs {opponent_name}",
                    f"📊 Score: {score}",
                    competition_line,
                ]
                if stadium_line:
                    lines.append(stadium_line)
                events.append(MatchEvent(f"{match_id}:halftime", "\n".join(lines)))
            else:
                lines = [
                    f"🔴 **Match is live:** {hashtag_name} vs {opponent_name}",
                    f"📊 Live score: {score}",
                    competition_line,
                ]
                if stadium_line:
                    lines.append(stadium_line)
                events.append(MatchEvent(f"{match_id}:live", "\n".join(lines)))
            continue

        if finished:
            lines = [
                f"✅ **Full-time:** {hashtag_name} vs {opponent_name}",
                f"📊 Final score: {score}",
                competition_line,
            ]
            if stadium_line:
                lines.append(stadium_line)
            events.append(MatchEvent(f"{match_id}:fulltime", "\n".join(lines)))

    return events


def post_to_discord(webhook_url: str, message: str) -> None:
    """Send a single message to Discord webhook."""
    _request_json(webhook_url, body={"content": message})


def run() -> int:
    """Main entry point for local runs and GitHub Actions."""
    dry_run = env_as_bool("DRY_RUN", default=False)
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    team_id = int(get_env("TEAM_ID", "1186081"))
    prematch_window_minutes = int(get_env("PREMATCH_WINDOW_MINUTES", "120"))
    match_lookahead_hours = int(get_env("MATCH_LOOKAHEAD_HOURS", "24"))
    test_message = os.getenv("DISCORD_TEST_MESSAGE", "").strip()

    if not webhook_url and not dry_run:
        raise RuntimeError("Missing required environment variable: DISCORD_WEBHOOK_URL")

    # Optional manual webhook smoke-test mode.
    if test_message:
        if dry_run:
            print(f"[DRY_RUN] Would post test message: {test_message}")
        else:
            post_to_discord(webhook_url, test_message)
            print("Posted test message to Discord.")
        return 0

    fixtures = fetch_team_fixtures(team_id)
    events = build_events(fixtures, team_id, prematch_window_minutes, match_lookahead_hours)

    posted_event_ids = load_state()
    # Filter out events already posted in earlier runs.
    new_events = [event for event in events if event.event_id not in posted_event_ids]

    if not new_events:
        print("No new events to post.")
        return 0

    for event in new_events:
        if dry_run:
            print(f"[DRY_RUN] Would post: {event.event_id} -> {event.message}")
        else:
            post_to_discord(webhook_url, event.message)
            posted_event_ids.add(event.event_id)
            print(f"Posted: {event.event_id}")

    if not dry_run:
        save_state(posted_event_ids)

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
