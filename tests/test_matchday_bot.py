import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot import matchday_bot
from bot.matchday_bot import _pick_match_obj, _request_json, build_events, env_as_bool, match_score


def _fixture(match):
    return {"fixtures": {"allFixtures": {"fixtures": [{"match": match}]}}}


def _base_match(status_overrides=None, minutes_from_now=60):
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
        "id": 999,
        "home": {"id": 1186081, "name": "Hashtag United", "score": 1},
        "away": {"id": 123, "name": "Opponent", "score": 0},
        "status": status,
        "tournament": {"name": "League"},
        "roundName": "Round 1",
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

    def test_messages_are_in_english_and_use_london_time_label(self):
        fixtures = _fixture(_base_match(minutes_from_now=30))
        events = build_events(fixtures, 1186081, prematch_window_minutes=120)
        prematch = next(event for event in events if event.event_id.endswith(":prematch"))
        self.assertIn("Match soon", prematch.message)
        self.assertIn("Kickoff (London)", prematch.message)

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

    def test_request_json_handles_empty_response_body(self):
        with patch("bot.matchday_bot.urlopen", return_value=_DummyResponse(b"")):
            data = _request_json("https://example.com", body={"content": "hello"})
        self.assertEqual(data, {})


if __name__ == "__main__":
    unittest.main()
