"""Interpolated train positions: the geometry behind the animated map.

GTFS-RT carries delays, never coordinates. A train's position is therefore
derived: take where it was scheduled to be, shift by its observed delay, and
interpolate along the segment between the two stations it sits between.
"""
import datetime as dt

import pytest

from dbrt import analytics

pytestmark = pytest.mark.postgres

BERLIN_OFFSET = dt.timezone(dt.timedelta(hours=2))
SERVICE_DATE = dt.date(2026, 8, 18)


def _seed(warehouse, *, delay_at_a=0, delay_at_b=0):
    """Two stations 1 degree of longitude apart, one ICE running between them.

    A departs 10:00 local, B arrives 11:00 local, so the midpoint of the
    scheduled run is 10:30 local (08:30 UTC).
    """
    warehouse.execute("INSERT INTO routes (route_id, route_short_name, route_category) VALUES ('r1','ICE 1','ICE')")
    warehouse.execute("INSERT INTO trips (trip_id, route_id, service_id) VALUES ('t1','r1','s1')")
    warehouse.execute(
        "INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES "
        "('A','Alpha',50.0,8.0), ('B','Beta',50.0,9.0)"
    )
    warehouse.execute(
        "INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_seconds, departure_seconds) VALUES "
        "('t1',0,'A',36000,36000), ('t1',1,'B',39600,39600)"
    )
    feed_ts = dt.datetime(2026, 8, 18, 8, 0, tzinfo=dt.timezone.utc)
    for seq, stop_id, delay in ((0, "A", delay_at_a), (1, "B", delay_at_b)):
        warehouse.execute(
            """INSERT INTO stop_time_updates
               (trip_id, service_date, stop_sequence, feed_timestamp, stop_id,
                arrival_delay, departure_delay, schedule_relationship, route_category)
               VALUES ('t1', %s, %s, %s, %s, %s, %s, 'SCHEDULED', 'ICE')""",
            (SERVICE_DATE, seq, feed_ts, stop_id, delay, delay),
        )
    return feed_ts


def test_train_midway_through_its_run_sits_between_the_two_stations(warehouse):
    _seed(warehouse)
    at = dt.datetime(2026, 8, 18, 8, 30, tzinfo=dt.timezone.utc)  # 10:30 local

    positions = analytics.live_positions(warehouse, at=at)

    assert len(positions) == 1
    pos = positions[0]
    assert pos["trip_id"] == "t1"
    assert pos["lat"] == pytest.approx(50.0)
    assert pos["lon"] == pytest.approx(8.5, abs=0.01)
    assert pos["progress"] == pytest.approx(0.5, abs=0.02)
    assert pos["from_stop"] == "Alpha" and pos["to_stop"] == "Beta"


def test_a_delayed_train_is_further_back_along_the_segment(warehouse):
    """30 minutes late at both ends shifts the whole run 30 minutes later."""
    _seed(warehouse, delay_at_a=1800, delay_at_b=1800)
    at = dt.datetime(2026, 8, 18, 8, 30, tzinfo=dt.timezone.utc)

    pos = analytics.live_positions(warehouse, at=at)[0]

    assert pos["progress"] == pytest.approx(0.0, abs=0.02)
    assert pos["lon"] == pytest.approx(8.0, abs=0.02)
    assert pos["delay_seconds"] == 1800


def test_progress_is_always_clamped_to_the_segment(warehouse):
    _seed(warehouse)
    for at in (
        dt.datetime(2026, 8, 18, 7, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.timezone.utc),
    ):
        for pos in analytics.live_positions(warehouse, at=at):
            assert 0.0 <= pos["progress"] <= 1.0


def test_position_carries_delay_category_and_route_for_the_map_legend(warehouse):
    _seed(warehouse, delay_at_a=420, delay_at_b=420)
    pos = analytics.live_positions(warehouse, at=dt.datetime(2026, 8, 18, 8, 30, tzinfo=dt.timezone.utc))[0]

    assert pos["route_category"] == "ICE"
    assert pos["route_name"] == "ICE 1"
    assert pos["delay_seconds"] == 420


def test_bearing_points_from_the_previous_stop_toward_the_next(warehouse):
    """The map rotates each marker, so the heading has to be real."""
    _seed(warehouse)
    pos = analytics.live_positions(warehouse, at=dt.datetime(2026, 8, 18, 8, 30, tzinfo=dt.timezone.utc))[0]
    # Due east: A is west of B at the same latitude.
    assert pos["bearing"] == pytest.approx(90, abs=5)


def test_a_trip_with_no_coordinates_is_skipped_rather_than_plotted_at_null_island(warehouse):
    _seed(warehouse)
    warehouse.execute("UPDATE stops SET stop_lat = NULL, stop_lon = NULL WHERE stop_id = 'B'")
    assert analytics.live_positions(warehouse, at=dt.datetime(2026, 8, 18, 8, 30, tzinfo=dt.timezone.utc)) == []


def test_history_window_reports_the_span_available_for_the_time_slider(warehouse):
    _seed(warehouse)
    window = analytics.history_window(warehouse)
    assert window["start"] is not None and window["end"] is not None
    assert window["start"] <= window["end"]


def test_empty_warehouse_yields_no_positions_and_a_null_window(warehouse):
    assert analytics.live_positions(warehouse, at=dt.datetime.now(tz=dt.timezone.utc)) == []
    assert analytics.history_window(warehouse)["start"] is None


# --- staleness -------------------------------------------------------------

def test_a_train_that_finished_hours_ago_is_not_still_on_the_map(warehouse):
    """Without a lower bound every trip ever observed stays parked at its
    terminus, so the map fills with services that ended days ago."""
    _seed(warehouse)
    long_after = dt.datetime(2026, 8, 18, 20, 0, tzinfo=dt.timezone.utc)  # ~11h past arrival

    assert analytics.live_positions(warehouse, at=long_after) == []


def test_a_train_that_has_just_arrived_is_still_shown(warehouse):
    """Dropping arrivals the instant they land would make trains vanish at the
    terminus mid-journey for anyone watching."""
    _seed(warehouse)
    just_after = dt.datetime(2026, 8, 18, 9, 5, tzinfo=dt.timezone.utc)  # 5 min past arrival

    positions = analytics.live_positions(warehouse, at=just_after)

    assert len(positions) == 1
    assert positions[0]["status"] == "arrived"


def test_observations_older_than_the_service_window_are_ignored(warehouse):
    """A stale observation must not resurrect a trip into the live view."""
    _seed(warehouse)
    days_later = dt.datetime(2026, 8, 21, 8, 30, tzinfo=dt.timezone.utc)

    assert analytics.live_positions(warehouse, at=days_later) == []


def test_a_running_train_is_unaffected_by_the_staleness_filter(warehouse):
    _seed(warehouse)
    midway = dt.datetime(2026, 8, 18, 8, 30, tzinfo=dt.timezone.utc)

    positions = analytics.live_positions(warehouse, at=midway)

    assert len(positions) == 1
    assert positions[0]["status"] == "running"
