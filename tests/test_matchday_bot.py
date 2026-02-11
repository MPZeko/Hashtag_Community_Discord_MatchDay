import unittest
from datetime import datetime, timedelta, timezone

from bot.matchday_bot import build_events


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


if __name__ == "__main__":
    unittest.main()
