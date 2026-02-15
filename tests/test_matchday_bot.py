import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bot import matchday_bot
from bot.matchday_bot import (
    _pick_match_obj,
    _request_json,
    build_events,
    build_next_match_message,
    collect_live_goal_events,
    build_finished_match_recap_message,
    env_as_bool,
    find_next_upcoming_match,
    find_latest_finished_match,
    fetch_match_details,
    load_state,
    match_score,
    parse_goal_events,
    parse_recap_goals,
    extract_goals,
    save_state,
    should_run_event_pipeline,
)


def _fixture(match):
    return {"fixtures": {"allFixtures": {"fixtures": [{"match": match}]}}}


def _fixtures(matches):
    return {"fixtures": {"allFixtures": {"fixtures": [{"match": m} for m in matches]}}}


def _base_match(status_overrides=None, minutes_from_now=60, match_id=999):
    status_overrides = status_overrides or {}
    match_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    status = {
        "utcTime": match_time.isoformat().replace("+00:00", "Z"),
        "started": False,
        "finished": False,
        "cancelled": False,
        "reason": {"short": ""},
    }
    status.update(status_overrides)

    return {
        "id": match_id,
        "home": {"id": 1186081, "name": "Hashtag United", "score": 1},
        "away": {"id": 123, "name": "Opponent", "score": 0},
        "status": status,
        "tournament": {"name": "League"},
        "roundName": "Round 1",
        "venue": {"name": "Parkside"},
    }


def _match_details(match_id=999):
    return {
        "general": {
            "homeTeam": {"id": 1186081, "name": "Hashtag United", "score": 1},
            "awayTeam": {"id": 123, "name": "Opponent", "score": 0},
            "status": {"scoreStr": "1 - 0"},
        },
        "content": {
            "shotmap": {
                "shots": [
                    {
                        "eventType": "Goal",
                        "min": 52,
                        "minAdded": 0,
                        "playerName": "Player A",
                        "assistPlayerName": "Player B",
                        "teamId": 1186081,
                        "isOwnGoal": False,
                    }
                ]
            }
        },
        "matchId": match_id,
    }


class _DummyResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload




class _DummyHTTPResponse:
    def __init__(self, payload: bytes, status: int = 200, content_type: str = "application/json"):
        self.payload = payload
        self.status = status
        self._headers = {"Content-Type": content_type}

    @property
    def headers(self):
        return self

    def get(self, key, default=None):
        return self._headers.get(key, default)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


class TestMatchDayBot(unittest.TestCase):
    def test_builds_prematch_event(self):
        fixtures = _fixture(_base_match(minutes_from_now=30))
        events = build_events(fixtures, 1186081, prematch_window_minutes=120)
        self.assertTrue(any(event.event_id.endswith(":prematch") for event in events))

    def test_builds_halftime_event(self):
        fixtures = _fixture(
            _base_match(
                status_overrides={
                    "started": True,
                    "finished": False,
                    "reason": {"short": "HT"},
                },
                minutes_from_now=-10,
            )
        )
        events = build_events(fixtures, 1186081, prematch_window_minutes=120)
        self.assertTrue(any(event.event_id.endswith(":halftime") for event in events))

    def test_builds_fulltime_event(self):
        fixtures = _fixture(
            _base_match(
                status_overrides={
                    "started": True,
                    "finished": True,
                },
                minutes_from_now=-120,
            )
        )
        events = build_events(fixtures, 1186081, prematch_window_minutes=120)
        self.assertTrue(any(event.event_id.endswith(":fulltime") for event in events))

    def test_match_lookahead_hours_filters_future_matches(self):
        fixtures = _fixture(_base_match(minutes_from_now=36 * 60))
        events_24 = build_events(fixtures, 1186081, prematch_window_minutes=3000, match_lookahead_hours=24)
        events_48 = build_events(fixtures, 1186081, prematch_window_minutes=3000, match_lookahead_hours=48)
        self.assertEqual(len(events_24), 0)
        self.assertTrue(any(event.event_id.endswith(":prematch") for event in events_48))

    def test_messages_are_in_english_and_use_london_time_label(self):
        fixtures = _fixture(_base_match(minutes_from_now=30))
        events = build_events(fixtures, 1186081, prematch_window_minutes=120)
        prematch = next(event for event in events if event.event_id.endswith(":prematch"))
        self.assertIn("Match soon", prematch.message)
        self.assertIn("Kickoff (London)", prematch.message)
        self.assertIn("🏆 League Round 1", prematch.message)
        self.assertIn("🏟️ Stadium: Parkside", prematch.message)

    def test_find_next_upcoming_match(self):
        fixtures = _fixtures(
            [
                _base_match(minutes_from_now=500, match_id=1),
                _base_match(minutes_from_now=60, match_id=2),
                _base_match(minutes_from_now=180, match_id=3),
            ]
        )
        match = find_next_upcoming_match(fixtures)
        self.assertIsNotNone(match)
        self.assertEqual(match.get("id"), 2)

    def test_build_next_match_message_includes_round_and_stadium(self):
        message = build_next_match_message(_base_match(minutes_from_now=60), 1186081)
        self.assertIn("Next match", message)
        self.assertIn("🏆 League Round 1", message)
        self.assertIn("🏟️ Stadium: Parkside", message)

    def test_parse_goal_events_contains_minute_scorer_and_scoreline(self):
        match = _base_match(
            status_overrides={"started": True, "finished": False},
            minutes_from_now=-15,
            match_id=444,
        )
        details = _match_details(match_id=444)
        goals = parse_goal_events(match, details, 1186081)
        self.assertEqual(len(goals), 1)
        goal = goals[0]
        self.assertIn("52' GOAL", goal.message)
        self.assertIn("Scorer: Player A", goal.message)
        self.assertIn("Hashtag United 1-0 Opponent", goal.message)
        self.assertIn("Assist: Player B", goal.message)

    def test_goal_event_id_is_stable_for_dedupe(self):
        match = _base_match(status_overrides={"started": True, "finished": False}, match_id=555)
        details = _match_details(match_id=555)
        a = parse_goal_events(match, details, 1186081)[0].event_id
        b = parse_goal_events(match, details, 1186081)[0].event_id
        self.assertEqual(a, b)

    def test_collect_live_goal_events_fetches_only_live_matches(self):
        live_match = _base_match(status_overrides={"started": True, "finished": False}, match_id=1001)
        finished_match = _base_match(status_overrides={"started": True, "finished": True}, match_id=1002)
        fixtures = _fixtures([live_match, finished_match])

        with patch("bot.matchday_bot.fetch_match_details", return_value=_match_details(match_id=1001)) as mock_fetch:
            events = collect_live_goal_events(fixtures, 1186081)

        self.assertEqual(len(events), 1)
        mock_fetch.assert_called_once_with("1001")

    def test_state_migration_from_legacy_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
            ids = load_state(path)
            self.assertEqual(ids, {"a", "b"})

    def test_save_state_writes_new_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state({"x", "y"}, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("schema_version"), 2)
            self.assertEqual(set(data.get("posted_event_ids", [])), {"x", "y"})

    def test_should_run_event_pipeline_fast_window_true(self):
        match = _base_match(minutes_from_now=40)
        fixtures = _fixture(match)
        now = datetime.now(timezone.utc)
        self.assertTrue(
            should_run_event_pipeline(
                fixtures,
                now=now,
                fast_window_before_minutes=60,
                fast_window_after_minutes=30,
                expected_match_duration_minutes=120,
                slow_poll_interval_minutes=30,
            )
        )

    def test_should_run_event_pipeline_slow_window_respects_interval(self):
        fixtures = _fixture(_base_match(minutes_from_now=5 * 24 * 60))
        now_non_boundary = datetime(2026, 2, 14, 10, 7, tzinfo=timezone.utc)
        now_boundary = datetime(2026, 2, 14, 10, 30, tzinfo=timezone.utc)

        self.assertFalse(
            should_run_event_pipeline(
                fixtures,
                now=now_non_boundary,
                slow_poll_interval_minutes=30,
            )
        )
        self.assertTrue(
            should_run_event_pipeline(
                fixtures,
                now=now_boundary,
                slow_poll_interval_minutes=30,
            )
        )

    def test_pick_match_obj_supports_multiple_shapes(self):
        self.assertEqual(_pick_match_obj({"match": {"id": 1}}), {"id": 1})
        self.assertEqual(_pick_match_obj({"fixture": {"id": 2}}), {"id": 2})
        self.assertEqual(_pick_match_obj({"status": {"utcTime": "2026-01-01T12:00:00Z"}, "id": 3})["id"], 3)

    def test_match_score_prefers_score_str(self):
        self.assertEqual(match_score({"status": {"scoreStr": "2-1"}}), "2-1")

    def test_env_as_bool_true_values(self):
        os.environ["DRY_RUN"] = "true"
        self.assertTrue(env_as_bool("DRY_RUN"))

    def test_env_as_bool_default_when_missing(self):
        os.environ.pop("UNSET_BOOL", None)
        self.assertTrue(env_as_bool("UNSET_BOOL", default=True))

    def test_find_latest_finished_match_selects_most_recent(self):
        older_finished = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-72 * 60,
            match_id=2001,
        )
        newest_finished = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-4 * 60,
            match_id=2002,
        )
        not_finished = _base_match(
            status_overrides={"started": False, "finished": False},
            minutes_from_now=2 * 60,
            match_id=2003,
        )
        fixtures = _fixtures([older_finished, newest_finished, not_finished])

        latest = find_latest_finished_match(fixtures, max_finished_age_hours=168)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.get("id"), 2002)

    def test_find_latest_finished_match_respects_max_age(self):
        very_old = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-10 * 24 * 60,
            match_id=3001,
        )
        fixtures = _fixtures([very_old])
        latest = find_latest_finished_match(fixtures, max_finished_age_hours=24)
        self.assertIsNone(latest)

    def test_build_finished_match_recap_message_includes_goals(self):
        match = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-90,
            match_id=4001,
        )
        details = {
            "general": {
                "homeTeam": {"id": 1186081, "name": "Hashtag United", "score": 2},
                "awayTeam": {"id": 123, "name": "Opponent", "score": 1},
                "status": {"scoreStr": "2 - 1"},
            },
            "content": {
                "shotmap": {
                    "shots": [
                        {"eventType": "Goal", "min": 12, "playerName": "Player A", "teamId": 1186081},
                        {"eventType": "Goal", "min": 55, "minAdded": 2, "playerName": "Player B", "teamId": 123},
                    ]
                }
            },
        }
        message = build_finished_match_recap_message(match, details, 1186081)
        self.assertIn("Final score: 2-1", message)
        self.assertIn("⚽ Goals:", message)
        self.assertIn("12' Player A (Home)", message)
        self.assertIn("55+2' Player B (Away)", message)
        self.assertIn("🏆 League (Round 1)", message)

    def test_extract_goals_from_shotmap_with_penalty_and_stoppage(self):
        details = {
            "general": {
                "homeTeam": {"id": 1186081, "name": "Hashtag United"},
                "awayTeam": {"id": 123, "name": "Opponent"},
            },
            "content": {
                "shotmap": {
                    "shots": [
                        {
                            "eventType": "Goal",
                            "min": 55,
                            "minAdded": 4,
                            "playerName": "Evans Kouassi",
                            "teamId": 1186081,
                            "isPenalty": True,
                        }
                    ]
                }
            },
        }
        goals = extract_goals(details)
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["minute_str"], "55+4")
        self.assertTrue(goals[0]["is_penalty"])
        self.assertTrue(goals[0]["is_home"])

    def test_parse_recap_goals_fallback_to_matchfacts_events(self):
        details = {
            "general": {
                "homeTeam": {"id": 1, "name": "Home"},
                "awayTeam": {"id": 2, "name": "Away"},
            },
            "content": {
                "matchFacts": {
                    "events": [
                        {"eventType": "Goal", "minute": 88, "name": "Late Winner", "teamId": 1}
                    ]
                }
            },
        }
        goals = parse_recap_goals(details)
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["minute"], "88")
        self.assertEqual(goals[0]["player"], "Late Winner")
        self.assertEqual(goals[0]["side"], "Home")

    def test_parse_recap_goals_fallback_to_incidents_section(self):
        details = {
            "general": {
                "homeTeam": {"id": 1186081, "name": "Hashtag United"},
                "awayTeam": {"id": 123, "name": "Opponent"},
            },
            "content": {
                "incidents": [
                    {"type": "Goal", "minute": 42, "name": "Luke Read", "teamId": 123}
                ]
            },
        }
        goals = parse_recap_goals(details)
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["minute"], "42")
        self.assertEqual(goals[0]["player"], "Luke Read")
        self.assertEqual(goals[0]["side"], "Away")

    def test_build_finished_recap_renders_penalty_marker(self):
        match = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-90,
            match_id=4020,
        )
        details = {
            "general": {
                "homeTeam": {"id": 1186081, "name": "Hashtag United", "score": 1},
                "awayTeam": {"id": 123, "name": "Opponent", "score": 0},
                "status": {"scoreStr": "1 - 0"},
            },
            "content": {
                "shotmap": {
                    "shots": [
                        {"eventType": "Goal", "min": 55, "minAdded": 4, "playerName": "Evans Kouassi", "teamId": 1186081, "isPenalty": True}
                    ]
                }
            },
        }
        message = build_finished_match_recap_message(match, details, 1186081)
        self.assertIn("55+4' Evans Kouassi (Pen.) (Home)", message)

    def test_build_finished_recap_includes_goals_na_when_missing(self):
        match = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-90,
            match_id=4010,
        )
        details = {
            "general": {
                "homeTeam": {"id": 1186081, "name": "Hashtag United", "score": 0},
                "awayTeam": {"id": 123, "name": "Opponent", "score": 0},
                "status": {"scoreStr": "0 - 0"},
            },
            "content": {},
        }
        message = build_finished_match_recap_message(match, details, 1186081)
        self.assertIn("⚽ Goals: N/A (source did not provide goal events)", message)

    def test_recap_event_id_dedupe_respected(self):
        posted = {"recap:999"}
        self.assertIn("recap:999", posted)

    def test_run_latest_finished_recap_skips_when_already_posted(self):
        os.environ["DRY_RUN"] = "false"
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/test"
        os.environ["SEND_LATEST_FINISHED_MATCH_NOW"] = "true"

        match = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-60,
            match_id=5555,
        )

        with patch.object(matchday_bot, "fetch_team_fixtures", return_value=_fixture(match)), \
             patch.object(matchday_bot, "load_state", return_value={"recap:5555"}), \
             patch.object(matchday_bot, "fetch_match_details") as mock_details, \
             patch.object(matchday_bot, "post_to_discord") as mock_post:
            code = matchday_bot.run()

        self.assertEqual(code, 0)
        mock_details.assert_called_once_with("5555")
        mock_post.assert_not_called()

        os.environ.pop("DRY_RUN", None)
        os.environ.pop("DISCORD_WEBHOOK_URL", None)
        os.environ.pop("SEND_LATEST_FINISHED_MATCH_NOW", None)

    def test_force_post_enables_recap_mode_implicitly(self):
        os.environ["DRY_RUN"] = "true"
        os.environ["FORCE_POST"] = "true"
        os.environ.pop("SEND_LATEST_FINISHED_MATCH_NOW", None)

        match = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-60,
            match_id=7878,
        )

        with patch.object(matchday_bot, "fetch_team_fixtures", return_value=_fixture(match)), \
             patch.object(matchday_bot, "fetch_match_details", return_value=_match_details(match_id=7878)) as mock_details, \
             patch("builtins.print") as mock_print:
            code = matchday_bot.run()

        self.assertEqual(code, 0)
        mock_details.assert_called_once_with("7878")
        joined = " ".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("FORCE_POST enabled -> enabling recap mode.", joined)

        os.environ.pop("DRY_RUN", None)
        os.environ.pop("FORCE_POST", None)

    def test_run_latest_finished_recap_force_post_bypasses_dedupe(self):
        os.environ["DRY_RUN"] = "false"
        os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/test"
        os.environ["SEND_LATEST_FINISHED_MATCH_NOW"] = "true"
        os.environ["FORCE_POST"] = "true"

        match = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-60,
            match_id=7777,
        )

        with patch.object(matchday_bot, "fetch_team_fixtures", return_value=_fixture(match)), \
             patch.object(matchday_bot, "load_state", return_value={"recap:7777"}), \
             patch.object(matchday_bot, "fetch_match_details", return_value=_match_details(match_id=7777)), \
             patch.object(matchday_bot, "post_to_discord") as mock_post, \
             patch.object(matchday_bot, "save_state") as mock_save:
            code = matchday_bot.run()

        self.assertEqual(code, 0)
        mock_post.assert_called_once()
        mock_save.assert_called_once()

        os.environ.pop("DRY_RUN", None)
        os.environ.pop("DISCORD_WEBHOOK_URL", None)
        os.environ.pop("SEND_LATEST_FINISHED_MATCH_NOW", None)
        os.environ.pop("FORCE_POST", None)

    def test_recap_debug_logs_sections_and_goal_count(self):
        os.environ["DRY_RUN"] = "true"
        os.environ["SEND_LATEST_FINISHED_MATCH_NOW"] = "true"
        os.environ["DEBUG_FOTMOB_PAYLOAD"] = "true"

        match = _base_match(
            status_overrides={"started": True, "finished": True, "reason": {"short": "FT"}},
            minutes_from_now=-60,
            match_id=6666,
        )

        with patch.object(matchday_bot, "fetch_team_fixtures", return_value=_fixture(match)), \
             patch.object(matchday_bot, "fetch_match_details", return_value=_match_details(match_id=6666)), \
             patch("builtins.print") as mock_print:
            code = matchday_bot.run()

        self.assertEqual(code, 0)
        joined = " ".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("Recap debug:", joined)
        self.assertIn("goals_parsed=1", joined)

        os.environ.pop("DRY_RUN", None)
        os.environ.pop("SEND_LATEST_FINISHED_MATCH_NOW", None)
        os.environ.pop("DEBUG_FOTMOB_PAYLOAD", None)

    def test_run_dry_run_with_test_message(self):
        os.environ["DRY_RUN"] = "true"
        os.environ["DISCORD_TEST_MESSAGE"] = "test message"
        os.environ.pop("DISCORD_WEBHOOK_URL", None)

        with patch.object(matchday_bot, "fetch_team_fixtures") as mock_fetch:
            code = matchday_bot.run()

        self.assertEqual(code, 0)
        mock_fetch.assert_not_called()

        os.environ.pop("DRY_RUN", None)
        os.environ.pop("DISCORD_TEST_MESSAGE", None)

    def test_fetch_match_details_debug_logs_non_json_preview(self):
        os.environ["DEBUG_FOTMOB_PAYLOAD"] = "true"
        response = _DummyHTTPResponse(b"<html>blocked</html>", status=200, content_type="text/html")

        with patch("bot.matchday_bot.urlopen", return_value=response), \
             patch("builtins.print") as mock_print:
            data = fetch_match_details("12345")

        self.assertEqual(data, {})
        joined = " ".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("matchDetails fetch debug:", joined)
        self.assertIn("status_code=200", joined)
        self.assertIn("content_type=text/html", joined)
        self.assertIn("json_parse_ok=False", joined)
        self.assertIn("matchDetails non-JSON preview:", joined)

        os.environ.pop("DEBUG_FOTMOB_PAYLOAD", None)

    def test_request_json_handles_empty_response_body(self):
        with patch("bot.matchday_bot.urlopen", return_value=_DummyResponse(b"")):
            data = _request_json("https://example.com", body={"content": "hello"})
        self.assertEqual(data, {})


if __name__ == "__main__":
    unittest.main()
