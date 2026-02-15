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

FOTMOB_TEAM_FIXTURES_URL = "https://www.fotmob.com/api/teams"
FOTMOB_MATCH_DETAILS_URL = "https://www.fotmob.com/api/matchDetails"
STATE_FILE = Path(".state/posted_events.json")
LONDON_TZ = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class MatchEvent:
    event_id: str
    message: str


@dataclass(frozen=True)
class GoalEvent:
    event_id: str
    message: str


def get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_as_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_state(path: Path = STATE_FILE) -> set[str]:
    """Load posted IDs from state file, with backward-compatible migration."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"posted_event_ids": []}, indent=2), encoding="utf-8")
        return set()

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return set(str(x) for x in raw)
    if isinstance(raw, dict):
        ids = raw.get("posted_event_ids") or raw.get("event_ids") or []
        if isinstance(ids, list):
            return set(str(x) for x in ids)
    return set()


def save_state(event_ids: set[str], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "posted_event_ids": sorted(event_ids),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _request_json(url: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
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
    return _request_json(
        FOTMOB_TEAM_FIXTURES_URL,
        params={"id": team_id, "timezone": "Europe/London", "ccode3": "GBR"},
    )


def fetch_match_details(match_id: str) -> dict[str, Any]:
    return _request_json(FOTMOB_MATCH_DETAILS_URL, params={"matchId": match_id})


def _pick_match_obj(item: dict[str, Any]) -> dict[str, Any] | None:
    if "match" in item and isinstance(item["match"], dict):
        return item["match"]
    if "fixture" in item and isinstance(item["fixture"], dict):
        return item["fixture"]
    if "status" in item and isinstance(item["status"], dict):
        return item
    return None


def parse_match_utc(match: dict[str, Any]) -> datetime:
    status = match.get("status") or {}
    utc_time = status.get("utcTime")
    if not utc_time:
        raise KeyError("Missing status.utcTime in match payload")
    return datetime.fromisoformat(str(utc_time).replace("Z", "+00:00"))


def team_display_name(match: dict[str, Any], team_id: int) -> tuple[str, str]:
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
    tournament = (match.get("tournament") or {}).get("name") or "Unknown competition"
    round_text = match_round(match)
    return f"🏆 {tournament} {round_text}".strip()


def _extract_scoreline_from_details(details: dict[str, Any], fallback_match: dict[str, Any]) -> str:
    general = details.get("general") or {}

    home_team = general.get("homeTeam") or {}
    away_team = general.get("awayTeam") or {}
    home_score = home_team.get("score")
    away_score = away_team.get("score")
    if home_score is not None and away_score is not None:
        return f"{home_score}-{away_score}"

    status = general.get("status") or {}
    score_str = status.get("scoreStr")
    if score_str:
        cleaned = str(score_str).replace(" ", "")
        return cleaned

    return match_score(fallback_match)


def _goal_minute(shot: dict[str, Any]) -> str:
    minute = shot.get("min")
    added = shot.get("minAdded")
    if minute is None:
        return "?"
    if added not in (None, 0, "0"):
        return f"{minute}+{added}"
    return str(minute)


def _goal_team_side(shot: dict[str, Any], details: dict[str, Any]) -> str:
    general = details.get("general") or {}
    home_id = (general.get("homeTeam") or {}).get("id")
    away_id = (general.get("awayTeam") or {}).get("id")
    team_id = shot.get("teamId")
    if home_id is not None and team_id == home_id:
        return "home"
    if away_id is not None and team_id == away_id:
        return "away"
    return str(team_id or "unknown")


def parse_goal_events(match: dict[str, Any], details: dict[str, Any], team_id: int) -> list[GoalEvent]:
    shots = (((details.get("content") or {}).get("shotmap") or {}).get("shots") or [])
    if not isinstance(shots, list):
        return []

    home_name, away_name = team_display_name(match, team_id)
    scoreline = _extract_scoreline_from_details(details, match)
    match_id = str(match.get("id") or "unknown")

    events: list[GoalEvent] = []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        event_type = str(shot.get("eventType") or "")
        if event_type.lower() != "goal":
            continue

        minute_text = _goal_minute(shot)
        minute = shot.get("min")
        player_name = str(shot.get("playerName") or shot.get("name") or "Unknown scorer")
        assist_name = str(shot.get("assistPlayerName") or shot.get("assistName") or "").strip()
        own_goal = bool(shot.get("isOwnGoal"))
        side = _goal_team_side(shot, details)

        event_id = f"goal:{match_id}:{side}:{player_name}:{minute_text}:{int(own_goal)}"

        lines = [
            f"⚽️ {minute_text}' GOAL — {home_name} {scoreline} {away_name}",
            f"Scorer: {player_name}{' (OG)' if own_goal else ''}",
        ]
        if assist_name:
            lines.append(f"Assist: {assist_name}")

        events.append(GoalEvent(event_id=event_id, message="\n".join(lines)))

    return events


def find_next_upcoming_match(fixtures: dict[str, Any]) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    candidates: list[tuple[datetime, dict[str, Any]]] = []

    fixture_items = fixtures.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
    for item in fixture_items:
        match = _pick_match_obj(item)
        if not match:
            continue

        try:
            match_time = parse_match_utc(match)
        except KeyError:
            continue

        if match_time >= now:
            candidates.append((match_time, match))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def build_next_match_message(match: dict[str, Any], team_id: int) -> str:
    hashtag_name, opponent_name = team_display_name(match, team_id)
    kickoff_dt = parse_match_utc(match)
    kickoff_london = kickoff_dt.astimezone(LONDON_TZ).strftime("%d-%m-%Y %H:%M")
    competition_line = build_competition_line(match)
    stadium = match_stadium(match)

    lines = [
        f"📌 **Next match:** {hashtag_name} vs {opponent_name}",
        f"🕒 Kickoff (London): {kickoff_london}",
        competition_line,
    ]
    if stadium:
        lines.append(f"🏟️ Stadium: {stadium}")

    return "\n".join(lines)


def should_run_event_pipeline(
    fixtures: dict[str, Any],
    now: datetime | None = None,
    fast_window_before_minutes: int = 60,
    fast_window_after_minutes: int = 30,
    expected_match_duration_minutes: int = 120,
    slow_poll_interval_minutes: int = 30,
) -> bool:
    now = now or datetime.now(timezone.utc)

    fixture_items = fixtures.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
    for item in fixture_items:
        match = _pick_match_obj(item)
        if not match:
            continue

        try:
            kickoff = parse_match_utc(match)
        except KeyError:
            continue

        status = match.get("status") or {}
        if bool(status.get("started")) and not bool(status.get("finished")):
            return True

        window_start = kickoff - timedelta(minutes=fast_window_before_minutes)
        window_end = kickoff + timedelta(minutes=expected_match_duration_minutes + fast_window_after_minutes)
        if window_start <= now <= window_end:
            return True

    return (now.minute % slow_poll_interval_minutes) == 0


def build_events(
    fixtures: dict[str, Any],
    team_id: int,
    prematch_window_minutes: int,
    match_lookahead_hours: int = 24,
) -> list[MatchEvent]:
    now = datetime.now(timezone.utc)
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
            kickoff_london_dt = match_time.astimezone(LONDON_TZ)
            kickoff_london = kickoff_london_dt.strftime("%d-%m-%Y %H:%M")
            minutes_to_kickoff = int((match_time - now).total_seconds() // 60)
            lines = [
                f"📣 **Match soon:** {hashtag_name} vs {opponent_name}",
                f"🕒 Kickoff (London): {kickoff_london}",
                competition_line,
            ]
            if 0 <= minutes_to_kickoff <= 60:
                lines.append(f"⏳ Kickoff in {minutes_to_kickoff} minutes")
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


def collect_live_goal_events(fixtures: dict[str, Any], team_id: int) -> list[GoalEvent]:
    goal_events: list[GoalEvent] = []
    fixture_items = fixtures.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])

    for item in fixture_items:
        match = _pick_match_obj(item)
        if not match:
            continue

        status = match.get("status") or {}
        started = bool(status.get("started"))
        finished = bool(status.get("finished"))
        if not started or finished:
            continue

        match_id = str(match.get("id") or item.get("id") or item.get("matchId") or "")
        if not match_id:
            continue

        try:
            details = fetch_match_details(match_id)
            goals = parse_goal_events(match, details, team_id)
            goal_events.extend(goals)
            print(f"Live goal scan: matchId={match_id}, goals_found={len(goals)}")
        except RuntimeError as exc:
            print(f"Warning: matchDetails fetch failed for matchId={match_id}: {exc}")

    return goal_events


def post_to_discord(webhook_url: str, message: str) -> None:
    _request_json(webhook_url, body={"content": message})


def run() -> int:
    dry_run = env_as_bool("DRY_RUN", default=False)
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    team_id = int(get_env("TEAM_ID", "1186081"))
    prematch_window_minutes = int(get_env("PREMATCH_WINDOW_MINUTES", "120"))
    match_lookahead_hours = int(get_env("MATCH_LOOKAHEAD_HOURS", "24"))
    send_next_match_now = env_as_bool("SEND_NEXT_MATCH_NOW", default=False)
    fast_window_before_minutes = int(get_env("FAST_WINDOW_BEFORE_MINUTES", "60"))
    fast_window_after_minutes = int(get_env("FAST_WINDOW_AFTER_MINUTES", "30"))
    expected_match_duration_minutes = int(get_env("EXPECTED_MATCH_DURATION_MINUTES", "120"))
    slow_poll_interval_minutes = int(get_env("SLOW_POLL_INTERVAL_MINUTES", "30"))
    test_message = os.getenv("DISCORD_TEST_MESSAGE", "").strip()

    if not webhook_url and not dry_run:
        raise RuntimeError("Missing required environment variable: DISCORD_WEBHOOK_URL")

    if test_message:
        if dry_run:
            print(f"[DRY_RUN] Would post test message: {test_message}")
        else:
            post_to_discord(webhook_url, test_message)
            print("Posted test message to Discord.")
        return 0

    fixtures = fetch_team_fixtures(team_id)

    if send_next_match_now:
        next_match = find_next_upcoming_match(fixtures)
        if not next_match:
            print("No upcoming matches found in FotMob payload.")
            return 0

        message = build_next_match_message(next_match, team_id)
        if dry_run:
            print(f"[DRY_RUN] Would post next match -> {message}")
        else:
            post_to_discord(webhook_url, message)
            print("Posted next upcoming match to Discord.")
        return 0

    if not should_run_event_pipeline(
        fixtures,
        fast_window_before_minutes=fast_window_before_minutes,
        fast_window_after_minutes=fast_window_after_minutes,
        expected_match_duration_minutes=expected_match_duration_minutes,
        slow_poll_interval_minutes=slow_poll_interval_minutes,
    ):
        print("Skipping this 5-minute tick (outside fast window and not on slow interval boundary).")
        return 0

    events = build_events(fixtures, team_id, prematch_window_minutes, match_lookahead_hours)
    goal_events = collect_live_goal_events(fixtures, team_id)

    posted_event_ids = load_state()
    print(f"State loaded from {STATE_FILE}, ids={len(posted_event_ids)}")

    new_events = [event for event in events if event.event_id not in posted_event_ids]
    new_goal_events = [event for event in goal_events if event.event_id not in posted_event_ids]

    if not new_events and not new_goal_events:
        print("No new events to post.")
        return 0

    for event in [*new_events, *new_goal_events]:
        if dry_run:
            print(f"[DRY_RUN] Would post: {event.event_id} -> {event.message}")
        else:
            post_to_discord(webhook_url, event.message)
            posted_event_ids.add(event.event_id)
            print(f"Posted: {event.event_id}")

    if not dry_run:
        save_state(posted_event_ids)
        print(f"State saved to {STATE_FILE}, ids={len(posted_event_ids)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
