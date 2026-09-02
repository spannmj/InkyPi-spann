import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plugins.metra import gtfs  # noqa: E402

TZ = pytz.timezone("America/Chicago")

META = {
    "services": {
        # Weekdays for the whole window
        "WK": {"start": "20260101", "end": "20261231", "days": [True] * 5 + [False, False]},
        # Weekends for the whole window
        "WE": {"start": "20260101", "end": "20261231", "days": [False] * 5 + [True, True]},
        # A one-off holiday schedule, only added via calendar_dates
        "HOL": {"start": "20260101", "end": "20261231", "days": [False] * 7},
    },
    "exceptions": {
        # Labor Day: weekday service removed, holiday service added
        "20260907": {"WK": "2", "HOL": "1"},
    },
    "stops": {"EDISONPK": "Edison Park", "OTC": "Chicago OTC"},
}

ROUTE_INDEX = {
    "route_id": "UP-NW",
    "trips": [
        {
            "id": "UP-NW_UNW618_V4_D",
            "service": "WK",
            "headsign": "Chicago OTC",
            "dir": "1",
            "stops": [
                ["BARRINGTON", 24000, 24000, 1],
                ["EDISONPK", 26220, 26220, 5],   # 07:17
                ["OTC", 28080, 28080, 7],        # 07:48
            ],
        },
        {
            "id": "UP-NW_UNW626_V3_D",
            "service": "WK",
            "headsign": "Chicago OTC",
            "dir": "1",
            "stops": [
                ["EDISONPK", 27720, 27720, 5],   # 07:42
                ["OTC", 29580, 29580, 7],        # 08:13
            ],
        },
        {
            "id": "UP-NW_UNW680_V3_D",
            "service": "WK",
            "headsign": "Chicago OTC",
            "dir": "1",
            "stops": [
                ["EDISONPK", 88860, 88860, 5],   # 24:41 -> 00:41 next day
                ["OTC", 90600, 90600, 7],        # 25:10 -> 01:10 next day
            ],
        },
        {
            # Outbound: passes OTC before EDISONPK, so it must never match
            "id": "UP-NW_UNW601_V3_A",
            "service": "WK",
            "headsign": "Harvard",
            "dir": "0",
            "stops": [
                ["OTC", 26100, 26100, 1],        # 07:15
                ["EDISONPK", 27000, 27000, 4],   # 07:30
            ],
        },
        {
            "id": "UP-NW_UNW999_V1_W",
            "service": "WE",
            "headsign": "Chicago OTC",
            "dir": "1",
            "stops": [
                ["EDISONPK", 28800, 28800, 5],   # 08:00
                ["OTC", 30600, 30600, 7],        # 08:30
            ],
        },
    ],
}


def at(year, month, day, hour, minute):
    return TZ.localize(datetime(year, month, day, hour, minute))


class TestParseGtfsTime:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("00:00:00", 0),
            ("07:42:00", 27720),
            ("7:42", 27720),
            ("24:41:00", 88860),   # after-midnight trips
            ("25:10:00", 90600),
            ("", None),
            ("garbage", None),
        ],
    )
    def test_parse(self, value, expected):
        assert gtfs.parse_gtfs_time(value) == expected


class TestTrainNumber:
    @pytest.mark.parametrize(
        "trip_id,expected",
        [
            ("UP-NW_UNW626_V3_D", "626"),
            ("BNSF_BN1200_V4_A", "1200"),
            ("NO_DIGITS_HERE", "NO_DIGITS_HERE"),
        ],
    )
    def test_train_number(self, trip_id, expected):
        assert gtfs.train_number(trip_id) == expected


class TestActiveServiceIds:
    def test_weekday(self):
        assert gtfs.active_service_ids(META, date(2026, 9, 2)) == {"WK"}

    def test_weekend(self):
        assert gtfs.active_service_ids(META, date(2026, 9, 5)) == {"WE"}

    def test_calendar_dates_override(self):
        # Labor Day removes weekday service and adds the holiday service
        assert gtfs.active_service_ids(META, date(2026, 9, 7)) == {"HOL"}

    def test_outside_date_range(self):
        meta = {"services": {"OLD": {"start": "20250101", "end": "20250201", "days": [True] * 7}}}
        assert gtfs.active_service_ids(meta, date(2026, 9, 2)) == set()


class TestFindDepartures:
    def test_returns_next_inbound_trains(self):
        now = at(2026, 9, 2, 7, 15)  # Wednesday
        results = gtfs.find_departures(META, ROUTE_INDEX, "EDISONPK", "OTC", now, TZ, limit=2)

        assert [item["train"] for item in results] == ["618", "626"]
        assert results[0]["scheduled_departure"] == at(2026, 9, 2, 7, 17)
        assert results[0]["scheduled_arrival"] == at(2026, 9, 2, 7, 48)
        assert results[1]["scheduled_departure"] == at(2026, 9, 2, 7, 42)

    def test_skips_trains_that_already_left(self):
        now = at(2026, 9, 2, 7, 30)
        results = gtfs.find_departures(META, ROUTE_INDEX, "EDISONPK", "OTC", now, TZ, limit=1)
        assert results[0]["train"] == "626"

    def test_ignores_wrong_direction(self):
        now = at(2026, 9, 2, 0, 0)
        results = gtfs.find_departures(META, ROUTE_INDEX, "EDISONPK", "OTC", now, TZ, limit=10)
        assert "601" not in [item["train"] for item in results]

    def test_reverse_direction_matches_outbound_trip(self):
        now = at(2026, 9, 2, 0, 0)
        results = gtfs.find_departures(META, ROUTE_INDEX, "OTC", "EDISONPK", now, TZ, limit=10)
        # Today's and tomorrow's runs of the same trip, in chronological order
        assert [item["train"] for item in results] == ["601", "601"]
        assert results[0]["scheduled_departure"] == at(2026, 9, 2, 7, 15)
        assert results[0]["scheduled_arrival"] == at(2026, 9, 2, 7, 30)
        assert results[1]["scheduled_departure"] == at(2026, 9, 3, 7, 15)

    def test_handles_after_midnight_trips(self):
        # 24:41 on the Wednesday service day is 00:41 Thursday
        now = at(2026, 9, 3, 0, 30)
        results = gtfs.find_departures(META, ROUTE_INDEX, "EDISONPK", "OTC", now, TZ, limit=1)
        assert results[0]["train"] == "680"
        assert results[0]["scheduled_departure"] == at(2026, 9, 3, 0, 41)
        assert results[0]["service_date"] == date(2026, 9, 2)

    def test_rolls_over_to_next_service_day(self):
        # Late Friday night: Friday's 24:41 train runs first, then Saturday's
        # weekend-only trip. Both service calendars must be consulted.
        now = at(2026, 9, 4, 23, 0)
        results = gtfs.find_departures(META, ROUTE_INDEX, "EDISONPK", "OTC", now, TZ, limit=2)
        assert [item["train"] for item in results] == ["680", "999"]
        assert results[0]["scheduled_departure"] == at(2026, 9, 5, 0, 41)
        assert results[1]["scheduled_departure"] == at(2026, 9, 5, 8, 0)

    def test_respects_limit(self):
        now = at(2026, 9, 2, 0, 0)
        results = gtfs.find_departures(META, ROUTE_INDEX, "EDISONPK", "OTC", now, TZ, limit=2)
        assert len(results) == 2

    def test_no_matching_stops(self):
        now = at(2026, 9, 2, 7, 15)
        assert gtfs.find_departures(META, ROUTE_INDEX, "EDISONPK", "NOWHERE", now, TZ) == []
