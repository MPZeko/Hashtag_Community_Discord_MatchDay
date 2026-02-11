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

FOTMOB_TEAM_FIXTURES_URL = "https://www.fotmob.com/api/teams"
STATE_FILE = Path(".state/posted_events.json")


@dataclass(frozen=True)
class MatchEvent:
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
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data)


def save_state(event_ids: set[str], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(event_ids), indent=2), encoding="utf-8")


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
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"HTTP request failed: {exc}") from exc


def fetch_team_fixtures(team_id: int) -> dict[str, Any]:
    return _request_json(FOTMOB_TEAM_FIXTURES_URL, params={"id": team_id})


def parse_match_utc(match: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(match["status"]["utcTime"].replace("Z", "+00:00"))


def team_display_name(match: dict[str, Any], team_id: int) -> tuple[str, str]:
    home = match["home"]
    away = match["away"]
    if int(home["id"]) == team_id:
        return home["name"], away["name"]
    return away["name"], home["name"]


def match_score(match: dict[str, Any]) -> str:
    home_score = match.get("home", {}).get("score")
    away_score = match.get("away", {}).get("score")
    if home_score is None or away_score is None:
        return "-"
    return f"{home_score}-{away_score}"


def build_events(fixtures: dict[str, Any], team_id: int, prematch_window_minutes: int) -> list[MatchEvent]:
    now = datetime.now(timezone.utc)
    lower = now - timedelta(hours=4)
    upper = now + timedelta(hours=24)
    prematch_threshold = now + timedelta(minutes=prematch_window_minutes)

    events: list[MatchEvent] = []
    all_matches = fixtures.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])

    for wrapper in all_matches:
        match = wrapper.get("match")
        if not match:
            continue

        match_time = parse_match_utc(match)
        if not (lower <= match_time <= upper):
            continue

        hashtag_name, opponent_name = team_display_name(match, team_id)
        tournament = match.get("tournament", {}).get("name", "Unknown tournament")
        round_name = match.get("roundName") or ""
        status = match.get("status", {})
        started = bool(status.get("started"))
        finished = bool(status.get("finished"))
        cancelled = bool(status.get("cancelled"))
        reason = status.get("reason", {})
        reason_code = reason.get("short") or reason.get("long") or ""
        match_id = str(match.get("id"))
        score = match_score(match)

        if cancelled:
            event_id = f"{match_id}:cancelled"
            msg = (
                f"❌ **{hashtag_name} vs {opponent_name}** er aflyst.\n"
                f"🏆 {tournament} {round_name}".strip()
            )
            events.append(MatchEvent(event_id, msg))
            continue

        if not started and match_time <= prematch_threshold:
            kickoff = match_time.astimezone().strftime("%d-%m-%Y %H:%M")
            event_id = f"{match_id}:prematch"
            msg = (
                f"📣 **Kamp snart:** {hashtag_name} vs {opponent_name}\n"
                f"🕒 Kickoff: {kickoff}\n"
                f"🏆 {tournament} {round_name}".strip()
            )
            events.append(MatchEvent(event_id, msg))
            continue

        if started and not finished:
            live_code = str(reason_code).upper()
            if live_code == "HT":
                event_id = f"{match_id}:halftime"
                msg = (
                    f"⏸️ **Halvleg:** {hashtag_name} vs {opponent_name}\n"
                    f"📊 Stilling: {score}"
                )
                events.append(MatchEvent(event_id, msg))
            else:
                event_id = f"{match_id}:live"
                msg = (
                    f"🔴 **Kampen er i gang:** {hashtag_name} vs {opponent_name}\n"
                    f"📊 Live-stilling: {score}"
                )
                events.append(MatchEvent(event_id, msg))
            continue

        if finished:
            event_id = f"{match_id}:fulltime"
            msg = (
                f"✅ **Slut:** {hashtag_name} vs {opponent_name}\n"
                f"📊 Slutresultat: {score}\n"
                f"🏆 {tournament} {round_name}".strip()
            )
            events.append(MatchEvent(event_id, msg))

    return events


def post_to_discord(webhook_url: str, message: str) -> None:
    _request_json(webhook_url, body={"content": message})


def run() -> int:
    dry_run = env_as_bool("DRY_RUN", default=False)
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    team_id = int(get_env("TEAM_ID", "1186081"))
    prematch_window_minutes = int(get_env("PREMATCH_WINDOW_MINUTES", "120"))

    if not webhook_url and not dry_run:
        raise RuntimeError("Missing required environment variable: DISCORD_WEBHOOK_URL")

    fixtures = fetch_team_fixtures(team_id)
    events = build_events(fixtures, team_id, prematch_window_minutes)

    posted_event_ids = load_state()
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
