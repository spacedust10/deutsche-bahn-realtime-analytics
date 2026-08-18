"""Scheduled stop times: the geometry and clock behind train positions.

GTFS encodes times past midnight as hours >= 24 ("25:10:00" is 01:10 the next
service day). Getting that wrong silently teleports late-night trains, so it is
pinned here.
"""
import datetime as dt

import pytest

from dbrt.static_gtfs import StaticTimetable, parse_gtfs_time


@pytest.fixture(scope="module")
def timetable(static_zip):
    return StaticTimetable.from_zip(static_zip, load_stop_times=True)


@pytest.mark.parametrize(
    "raw,expected_seconds",
    [
        ("00:00:00", 0),
        ("08:30:00", 8 * 3600 + 30 * 60),
        ("23:59:59", 86399),
        ("24:00:00", 86400),          # midnight at the end of the service day
        ("25:10:00", 25 * 3600 + 600),
        ("31:57:00", 31 * 3600 + 57 * 60),  # real value from the DB feed
    ],
)
def test_gtfs_time_is_seconds_since_service_day_start(raw, expected_seconds):
    assert parse_gtfs_time(raw) == expected_seconds


def test_blank_or_malformed_time_is_none():
    assert parse_gtfs_time("") is None
    assert parse_gtfs_time(None) is None
    assert parse_gtfs_time("not-a-time") is None


def test_stop_times_are_loaded_only_when_requested(static_zip):
    assert StaticTimetable.from_zip(static_zip).stop_times == {}


def test_stop_times_group_by_trip_in_stop_sequence_order(timetable):
    assert timetable.stop_times
    for calls in timetable.stop_times.values():
        sequences = [c.stop_sequence for c in calls]
        assert sequences == sorted(sequences)


def test_a_call_carries_its_stop_and_both_scheduled_times(timetable):
    calls = next(iter(timetable.stop_times.values()))
    first = calls[0]
    assert first.stop_id
    assert first.departure_seconds is not None


def test_scheduled_times_increase_along_a_trip(timetable):
    """A route whose clock goes backwards would break any interpolation."""
    for calls in timetable.stop_times.values():
        times = [c.departure_seconds for c in calls if c.departure_seconds is not None]
        assert times == sorted(times)


def test_network_edges_are_consecutive_stop_pairs_with_coordinates(timetable):
    edges = timetable.network_edges()
    assert edges
    for (a, b) in edges:
        assert a in timetable.stops and b in timetable.stops


def test_network_edges_are_deduplicated_across_trips(timetable):
    edges = timetable.network_edges()
    assert len(edges) == len(set(edges))


def test_absolute_time_combines_service_date_with_gtfs_offset():
    from dbrt.static_gtfs import absolute_time

    got = absolute_time(dt.date(2026, 8, 18), 25 * 3600 + 600)
    assert got.date() == dt.date(2026, 8, 19)
    assert got.hour == 1 and got.minute == 10
    assert got.tzinfo is not None
