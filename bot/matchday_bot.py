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
    params = {"matchId": match_id}
    full_url = f"{FOTMOB_MATCH_DETAILS_URL}?{urlencode(params)}"
    req = Request(
        full_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
        method="GET",
    )

    debug = env_as_bool("DEBUG_FOTMOB_PAYLOAD", default=False)

    try:
        with urlopen(req, timeout=10) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
            decoded = raw.decode("utf-8", errors="replace") if raw else ""

            try:
                parsed = json.loads(decoded) if decoded.strip() else {}
                json_ok = True
            except json.JSONDecodeError:
                parsed = {}
                json_ok = False

            if debug:
                print(
                    "matchDetails fetch debug: "
                    f"status_code={status_code}, "
                    f"content_type={content_type}, "
                    f"json_parse_ok={json_ok}"
                )
                if not json_ok:
                    print(f"matchDetails non-JSON preview: {decoded[:200]}")

            return parsed
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"HTTP request failed: {exc}") from exc


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


def is_finished_match(match: dict[str, Any]) -> bool:
    status = match.get("status") or {}
    if bool(status.get("finished")):
        return True
    reason = status.get("reason") or {}
    short = str(reason.get("short") or "").upper()
    long_text = str(reason.get("long") or "").lower()
    if short in {"FT", "AET", "PEN"}:
        return True
    return "full-time" in long_text or "full time" in long_text


def find_latest_finished_match(
    fixtures: dict[str, Any],
    max_finished_age_hours: int,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    earliest = now - timedelta(hours=max_finished_age_hours)
    candidates: list[tuple[datetime, dict[str, Any]]] = []

    fixture_items = fixtures.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
    for item in fixture_items:
        match = _pick_match_obj(item)
        if not match or not is_finished_match(match):
            continue

        try:
            match_time = parse_match_utc(match)
        except KeyError:
            continue

        if earliest <= match_time <= now:
            candidates.append((match_time, match))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _goal_side_label_for_recap(event: dict[str, Any], details: dict[str, Any]) -> str:
    """Return Home/Away label for recap goal lines."""
    team_id = event.get("teamId")
    if team_id is None and isinstance(event.get("team"), dict):
        team_id = event["team"].get("id")
    general = details.get("general") or {}
    home_id = (general.get("homeTeam") or {}).get("id")
    away_id = (general.get("awayTeam") or {}).get("id")

    if team_id is not None:
        if home_id is not None and team_id == home_id:
            return "Home"
        if away_id is not None and team_id == away_id:
            return "Away"

    side = str(event.get("side") or event.get("team") or event.get("homeAway") or "").lower()
    if side.startswith("home"):
        return "Home"
    if side.startswith("away"):
        return "Away"
    return "Unknown"


def _extract_event_type(event: dict[str, Any]) -> str:
    return str(
        event.get("eventType")
        or event.get("type")
        or event.get("event")
        or event.get("incidentType")
        or event.get("eventName")
        or ""
    ).lower()


def _extract_minute_parts(event: dict[str, Any]) -> tuple[int, int, str]:
    minute = event.get("min")
    if minute is None:
        minute = event.get("minute")
    if minute is None:
        minute = event.get("time")

    added = event.get("minAdded")
    if added is None:
        added = event.get("addedTime")

    try:
        minute_base = int(minute)
    except (TypeError, ValueError):
        minute_base = 0
    try:
        minute_added = int(added or 0)
    except (TypeError, ValueError):
        minute_added = 0

    if minute is None:
        minute_text = "?"
    elif minute_added > 0:
        minute_text = f"{minute_base}+{minute_added}"
    else:
        minute_text = str(minute_base)

    return minute_base, minute_added, minute_text


def _extract_player_name(event: dict[str, Any]) -> str:
    if event.get("playerName"):
        return str(event["playerName"])
    if event.get("name"):
        return str(event["name"])
    if isinstance(event.get("player"), dict) and event["player"].get("name"):
        return str(event["player"]["name"])
    if isinstance(event.get("participant"), dict) and event["participant"].get("name"):
        return str(event["participant"]["name"])
    return "Unknown scorer"


def _is_goal_like_event(event: dict[str, Any]) -> bool:
    event_type = _extract_event_type(event)
    if "goal" in event_type:
        return True

    has_player = bool(event.get("playerName") or event.get("name") or isinstance(event.get("player"), dict))
    has_time = any(event.get(k) is not None for k in ("min", "minute", "time"))
    return has_player and has_time


def _extract_event_list_at_path(payload: dict[str, Any], path: tuple[str, ...]) -> list[dict[str, Any]]:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    if isinstance(node, list):
        return [x for x in node if isinstance(x, dict)]
    return []


def _collect_goal_event_candidates(details: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = {}
    paths = [
        ("content", "shotmap", "shots"),
        ("content", "matchFacts", "events"),
        ("content", "matchFacts", "events", "events"),
        ("content", "events"),
        ("content", "incidents"),
    ]
    for path in paths:
        events = _extract_event_list_at_path(details, path)
        if events:
            candidates[".".join(path)] = events
    return candidates


def log_match_details_presence(details: dict[str, Any]) -> None:
    top_keys = sorted(details.keys()) if isinstance(details, dict) else []
    content = details.get("content") if isinstance(details, dict) else None
    content_keys = sorted(content.keys()) if isinstance(content, dict) else []

    shotmap = (content.get("shotmap") if isinstance(content, dict) else {}) or {}
    shots = shotmap.get("shots") if isinstance(shotmap, dict) else []
    shot_count = len(shots) if isinstance(shots, list) else 0

    candidates = _collect_goal_event_candidates(details)
    event_counts = {name: len(items) for name, items in candidates.items() if name != "content.shotmap.shots"}
    has_any_events_section = bool(event_counts)

    print(f"Recap payload presence: top_level_keys={top_keys}")
    print(f"Recap payload presence: content_keys={content_keys}")
    print(
        "Recap payload presence: "
        f"has_shotmap={shot_count > 0}, shot_count={shot_count}, "
        f"has_any_events_section={has_any_events_section}, event_counts={event_counts}"
    )


def extract_goals(match_details: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract recap goals from matchDetails using shotmap then fallback event sections."""
    candidates = _collect_goal_event_candidates(match_details)

    ordered_sources = [
        "content.shotmap.shots",
        "content.matchFacts.events",
        "content.matchFacts.events.events",
        "content.events",
        "content.incidents",
    ]

    source_events: list[dict[str, Any]] = []
    for source in ordered_sources:
        events = candidates.get(source, [])
        if events:
            source_events = events
            break

    goals: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for event in source_events:
        if not _is_goal_like_event(event):
            continue

        minute_base, minute_added, minute_text = _extract_minute_parts(event)
        goals.append(
            (
                (minute_base, minute_added),
                {
                    "minute_str": minute_text,
                    "player_name": _extract_player_name(event),
                    "is_home": _goal_side_label_for_recap(event, match_details) == "Home",
                    "is_penalty": bool(event.get("isPenalty") or event.get("penalty")),
                    "is_own_goal": bool(event.get("isOwnGoal") or event.get("ownGoal")),
                },
            )
        )

    goals.sort(key=lambda item: item[0])
    return [goal for _, goal in goals]


def parse_recap_goals(details: dict[str, Any]) -> list[dict[str, str]]:
    goals = extract_goals(details)
    parsed: list[dict[str, str]] = []
    for goal in goals:
        parsed.append(
            {
                "minute": str(goal["minute_str"]),
                "player": str(goal["player_name"]),
                "side": "Home" if goal["is_home"] else "Away",
                "own_goal": "true" if goal["is_own_goal"] else "false",
                "is_penalty": "true" if goal["is_penalty"] else "false",
            }
        )
    return parsed


def build_recap_competition_line(match: dict[str, Any], details: dict[str, Any]) -> str:
    general = details.get("general") or {}
    competition = (
        (match.get("tournament") or {}).get("name")
        or general.get("parentLeagueName")
        or general.get("leagueName")
        or "Unknown competition"
    )
    stage = (
        match_round(match)
        or general.get("leagueRoundName")
        or general.get("roundName")
        or ""
    )
    if stage:
        return f"🏆 {competition} ({stage})"
    return f"🏆 {competition}"


def build_finished_match_recap_message(match: dict[str, Any], details: dict[str, Any], team_id: int) -> str:
    home_name = str((match.get("home") or {}).get("name") or (details.get("general") or {}).get("homeTeam", {}).get("name") or "Home")
    away_name = str((match.get("away") or {}).get("name") or (details.get("general") or {}).get("awayTeam", {}).get("name") or "Away")
    kickoff_dt = parse_match_utc(match)
    kickoff_london = kickoff_dt.astimezone(LONDON_TZ).strftime("%d-%m-%Y %H:%M")
    scoreline = _extract_scoreline_from_details(details, match)
    competition_line = build_recap_competition_line(match, details)
    stadium = match_stadium(match)
    if not stadium:
        stadium = str((details.get("general") or {}).get("venue", {}).get("name") or "")

    lines = [
        f"✅ **Full-time:** {home_name} vs {away_name}",
        f"📊 Final score: {scoreline}",
        f"🕒 Kickoff (London): {kickoff_london}",
        competition_line,
    ]
    if stadium:
        lines.append(f"🏟️ {stadium}")

    goals = parse_recap_goals(details)
    if goals:
        lines.append("⚽ Goals:")
        for goal in goals:
            extras: list[str] = []
            if goal.get("is_penalty") == "true":
                extras.append("Pen.")
            if goal.get("own_goal") == "true":
                extras.append("OG")
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"- {goal['minute']}' {goal['player']}{suffix} ({goal['side']})")
    else:
        lines.append("⚽ Goals: N/A (source did not provide goal events)")

    return "\n".join(lines)


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
    send_latest_finished_match_now = env_as_bool("SEND_LATEST_FINISHED_MATCH_NOW", default=False)
    force_post = env_as_bool("FORCE_POST", default=False)
    if force_post and not send_latest_finished_match_now:
        send_latest_finished_match_now = True
        print("FORCE_POST enabled -> enabling recap mode.")
    max_finished_age_hours = int(get_env("MAX_FINISHED_AGE_HOURS", "168"))
    debug_fotmob_payload = env_as_bool("DEBUG_FOTMOB_PAYLOAD", default=False)
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

    if send_latest_finished_match_now:
        latest_match = find_latest_finished_match(fixtures, max_finished_age_hours=max_finished_age_hours)
        if not latest_match:
            print("No recent finished match found.")
            return 0

        match_id = str(latest_match.get("id") or "")
        if not match_id:
            print("Latest finished match has no match id; skipping recap post.")
            return 0

        details = fetch_match_details(match_id)

        if debug_fotmob_payload:
            log_match_details_presence(details)
            recap_goals = extract_goals(details)
            print(f"Recap debug: goals_parsed={len(recap_goals)}")

        recap_event_id = f"recap:{match_id}"
        posted_event_ids = load_state()
        print(f"State loaded from {STATE_FILE}, ids={len(posted_event_ids)}")

        if recap_event_id in posted_event_ids and not force_post:
            print(f"Recap already posted for matchId={match_id}. Use FORCE_POST=true to repost.")
            return 0

        recap_message = build_finished_match_recap_message(latest_match, details, team_id)

        if dry_run:
            print(f"[DRY_RUN] Would post latest finished match recap -> {recap_message}")
        else:
            post_to_discord(webhook_url, recap_message)
            posted_event_ids.add(recap_event_id)
            save_state(posted_event_ids)
            print(f"Posted latest finished match recap for matchId={match_id}")
            print(f"State saved to {STATE_FILE}, ids={len(posted_event_ids)}")
        return 0

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
