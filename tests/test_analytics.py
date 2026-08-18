"""Analytical queries over the realtime history.

Punctuality uses Deutsche Bahn's own published definition: a stop counts as
punctual when it is less than 6 minutes (360 s) late.
"""
import datetime as dt

import pytest

from dbrt import analytics
from dbrt.gtfs_rt import StopTimeUpdateRecord

pytestmark = pytest.mark.postgres

BASE = dt.datetime(2026, 8, 18, 6, 0, tzinfo=dt.timezone.utc)
DAY = dt.date(2026, 8, 18)


def rec(trip, seq, arr, stop_id, ts=BASE, category="ICE"):
    return (
        StopTimeUpdateRecord(
            trip_id=trip, service_date=DAY, stop_sequence=seq, stop_id=stop_id,
            arrival_delay=arr, departure_delay=arr, arrival_time=ts, departure_time=ts,
            schedule_relationship="SCHEDULED", trip_schedule_relationship="SCHEDULED", route_id="1",
        ),
        ts, category,
    )


@pytest.fixture()
def seeded(warehouse):
    warehouse.insert_stop_time_updates([
        # Trip A: on time, then progressively late (delay propagation).
        rec("A", 0, 0, "S1"), rec("A", 1, 120, "S2"), rec("A", 2, 600, "S3"),
        # Trip B: consistently punctual.
        rec("B", 0, 0, "S1"), rec("B", 1, 60, "S2"),
        # Trip C: an IC running very late at the same stations.
        rec("C", 0, 900, "S1", category="IC"), rec("C", 1, 1200, "S2", category="IC"),
    ])
    with warehouse.conn.cursor() as cur:
        cur.execute("INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES "
                    "('S1','Berlin Hbf',52.525,13.369),('S2','Hannover Hbf',52.377,9.741),"
                    "('S3','Köln Hbf',50.943,6.958)")
    return warehouse


# --- punctuality -----------------------------------------------------------

def test_punctuality_uses_the_six_minute_threshold():
    assert analytics.PUNCTUALITY_THRESHOLD_SECONDS == 360


def test_overall_punctuality_counts_stops_under_the_threshold(seeded):
    result = analytics.punctuality(seeded)
    # 7 stops; late ones are A@600, C@900, C@1200 -> 4 of 7 punctual.
    assert result["total_stops"] == 7
    assert result["punctual_stops"] == 4
    assert result["punctuality_pct"] == pytest.approx(57.14, abs=0.01)


def test_punctuality_reports_mean_and_max_delay(seeded):
    result = analytics.punctuality(seeded)
    assert result["mean_delay_seconds"] == pytest.approx(411.43, abs=0.01)
    assert result["max_delay_seconds"] == 1200


def test_punctuality_on_empty_history_returns_zeroes_not_a_crash(warehouse):
    result = analytics.punctuality(warehouse)
    assert result["total_stops"] == 0
    assert result["punctuality_pct"] == 0.0


# --- station analysis ------------------------------------------------------

def test_station_delays_rank_worst_first_and_resolve_names(seeded):
    rows = analytics.station_delays(seeded, limit=10, min_observations=1)
    assert rows[0]["stop_name"] in {"Berlin Hbf", "Hannover Hbf"}
    assert rows[0]["mean_delay_seconds"] >= rows[-1]["mean_delay_seconds"]
    assert all("stop_name" in r for r in rows)


def test_station_delays_respect_the_minimum_observation_filter(seeded):
    """Ranking stations on a single observation produces noise, not insight."""
    assert analytics.station_delays(seeded, min_observations=3) == []


def test_station_delays_include_coordinates_for_mapping(seeded):
    rows = analytics.station_delays(seeded, min_observations=1)
    located = [r for r in rows if r["stop_lat"] is not None]
    assert located and all(45 < r["stop_lat"] < 58 for r in located)


# --- category / route reliability ------------------------------------------

def test_category_breakdown_separates_ice_from_ic(seeded):
    rows = {r["route_category"]: r for r in analytics.category_breakdown(seeded)}
    assert set(rows) == {"ICE", "IC"}
    assert rows["ICE"]["punctuality_pct"] > rows["IC"]["punctuality_pct"]


def test_category_breakdown_counts_trips_not_just_stops(seeded):
    rows = {r["route_category"]: r for r in analytics.category_breakdown(seeded)}
    assert rows["ICE"]["trips"] == 2
    assert rows["IC"]["trips"] == 1


# --- propagation -----------------------------------------------------------

def test_delay_propagation_returns_stops_in_travel_order(seeded):
    rows = analytics.delay_propagation(seeded, trip_id="A", service_date=DAY)
    assert [r["stop_sequence"] for r in rows] == [0, 1, 2]
    assert [r["arrival_delay"] for r in rows] == [0, 120, 600]
    assert rows[2]["stop_name"] == "Köln Hbf"


def test_delay_propagation_of_an_unknown_trip_is_empty(seeded):
    assert analytics.delay_propagation(seeded, trip_id="ZZZ", service_date=DAY) == []


def test_worst_delayed_trips_are_ordered_by_peak_delay(seeded):
    rows = analytics.worst_trips(seeded, limit=5)
    assert rows[0]["trip_id"] == "C"
    assert rows[0]["max_delay_seconds"] == 1200


# --- time series -----------------------------------------------------------

def test_delay_timeseries_buckets_observations_over_time(seeded):
    later = BASE + dt.timedelta(minutes=10)
    seeded.insert_stop_time_updates([rec("A", 3, 900, "S3", ts=later)])
    rows = analytics.delay_timeseries(seeded, bucket_minutes=5, hours=24)
    assert len(rows) >= 2
    assert all("bucket" in r and "mean_delay_seconds" in r for r in rows)
    assert rows[0]["bucket"] <= rows[-1]["bucket"], "series must be chronological"


def test_delay_timeseries_reports_punctuality_per_bucket(seeded):
    rows = analytics.delay_timeseries(seeded, bucket_minutes=5, hours=24)
    assert all(0 <= r["punctuality_pct"] <= 100 for r in rows)


def test_network_snapshot_lists_currently_tracked_trains(seeded):
    rows = analytics.network_snapshot(seeded)
    assert {r["trip_id"] for r in rows} == {"A", "B", "C"}
    assert all("current_delay_seconds" in r for r in rows)


def test_delay_distribution_buckets_delays_into_bands(seeded):
    rows = analytics.delay_distribution(seeded)
    labels = [r["band"] for r in rows]
    assert "on time" in labels[0].lower() or "<" in labels[0]
    assert sum(r["stops"] for r in rows) == 7
