"""Analytical queries over the realtime history.

Punctuality uses Deutsche Bahn's own published definition: a stop counts as
punctual when it is less than 6 minutes (360 s) late.
"""
import datetime as dt

import pytest

from dbrt import analytics
from dbrt.gtfs_rt import StopTimeUpdateRecord

pytestmark = pytest.mark.postgres

# Anchored to now, not to a calendar date. Every analytic filters on
# `now() - interval`, so fixtures pinned to a fixed day pass on the day they are
# written and silently start returning nothing once that day rolls out of the
# window. These tests are about relative recency; the actual date is irrelevant.
BASE = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=1)
DAY = BASE.date()


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
    # Means: Köln 600 (1 obs), Hannover 460 (3 obs), Berlin 300 (3 obs).
    assert [r["stop_name"] for r in rows] == ["Köln Hbf", "Hannover Hbf", "Berlin Hbf"]
    assert rows[0]["mean_delay_seconds"] >= rows[-1]["mean_delay_seconds"]


def test_station_delays_respect_the_minimum_observation_filter(seeded):
    """Ranking stations on a single observation produces noise, not insight:
    Köln has the worst mean delay but only one observation behind it."""
    names = [r["stop_name"] for r in analytics.station_delays(seeded, min_observations=3)]
    assert names == ["Hannover Hbf", "Berlin Hbf"]
    assert "Köln Hbf" not in names
    assert analytics.station_delays(seeded, min_observations=4) == []


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
    assert sum(r["stops"] for r in rows) == 7


def test_delay_distribution_returns_the_domain_bands_in_order(seeded):
    """Keys and order come from the domain layer, so the chart cannot invent a
    band the rest of the system does not recognise."""
    from dbrt import domain

    rows = analytics.delay_distribution(seeded)
    assert [r["band"] for r in rows] == [b.key for b in domain.DELAY_BANDS]
    assert all(r["label"] and r["severity"] is not None for r in rows)


def test_delay_distribution_keeps_empty_bands_so_bars_do_not_vanish(seeded):
    """A band nothing landed in this window is a zero, not a missing category;
    dropping it makes the histogram silently change shape."""
    rows = analytics.delay_distribution(seeded)
    assert len(rows) == len(analytics.domain.DELAY_BANDS)
    assert any(r["stops"] == 0 for r in rows)


# --- cancellations ---------------------------------------------------------

def test_cancellations_counts_skipped_stops_and_affected_trips(warehouse):
    warehouse.insert_stop_time_updates([rec("A", 0, 0, "S1"), rec("A", 1, 60, "S2")])
    with warehouse.conn.cursor() as cur:
        cur.execute("UPDATE stop_time_updates SET schedule_relationship='SKIPPED' "
                    "WHERE trip_id='A' AND stop_sequence=1")

    result = analytics.cancellations(warehouse)
    assert result["skipped_stops"] == 1
    assert result["affected_trips"] == 1
    assert result["total_stops"] == 2
    assert result["skipped_pct"] == 50.0


def test_cancellations_on_a_clean_network_are_zero_not_a_crash(warehouse):
    result = analytics.cancellations(warehouse)
    assert result == {"skipped_stops": 0, "affected_trips": 0, "cancelled_trips": 0,
                      "total_stops": 0, "skipped_pct": 0.0}


def test_cancelled_trips_are_counted_separately_from_skipped_stops(warehouse):
    warehouse.insert_stop_time_updates([rec("A", 0, 0, "S1"), rec("B", 0, 0, "S1")])
    with warehouse.conn.cursor() as cur:
        cur.execute("UPDATE stop_time_updates SET trip_schedule_relationship='CANCELED' WHERE trip_id='B'")

    result = analytics.cancellations(warehouse)
    assert result["cancelled_trips"] == 1
    assert result["skipped_stops"] == 0


def test_skipped_stations_rank_the_most_frequently_dropped_stops(warehouse):
    warehouse.insert_stop_time_updates([rec("A", 0, 0, "S1"), rec("B", 0, 0, "S1"), rec("C", 0, 0, "S2")])
    with warehouse.conn.cursor() as cur:
        cur.execute("INSERT INTO stops (stop_id, stop_name) VALUES ('S1','Berlin Hbf'),('S2','Fulda')")
        cur.execute("UPDATE stop_time_updates SET schedule_relationship='SKIPPED'")

    rows = analytics.skipped_stations(warehouse, limit=5)
    assert rows[0]["stop_name"] == "Berlin Hbf"
    assert rows[0]["skipped"] == 2


# --- station aggregation ---------------------------------------------------

def test_station_delays_aggregate_platforms_into_one_station(warehouse):
    """GTFS models each platform as its own stop_id under a parent station.
    Ranking platforms separately shows the same city twice with different
    numbers, which reads as a bug and hides the station's real total."""
    with warehouse.conn.cursor() as cur:
        cur.execute("INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon, parent_station) VALUES "
                    "('P',  'Göttingen', 51.536, 9.926, NULL),"
                    "('P1', 'Göttingen', 51.536, 9.926, 'P'),"
                    "('P2', 'Göttingen', 51.536, 9.926, 'P')")
    warehouse.insert_stop_time_updates([
        rec("A", 0, 600, "P1"), rec("B", 0, 1200, "P2"), rec("C", 0, 300, "P"),
    ])

    rows = analytics.station_delays(warehouse, min_observations=1)

    assert len(rows) == 1, f"expected one Göttingen, got {[r['stop_name'] for r in rows]}"
    assert rows[0]["stop_name"] == "Göttingen"
    assert rows[0]["observations"] == 3
    assert rows[0]["mean_delay_seconds"] == 700.0


def test_station_delays_keep_the_parent_station_coordinates(warehouse):
    with warehouse.conn.cursor() as cur:
        cur.execute("INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon, parent_station) VALUES "
                    "('P','Göttingen',51.536,9.926,NULL),('P1','Göttingen',51.536,9.926,'P')")
    warehouse.insert_stop_time_updates([rec("A", 0, 600, "P1")])

    row = analytics.station_delays(warehouse, min_observations=1)[0]
    assert row["stop_lat"] == pytest.approx(51.536)


def test_recent_polls_returns_ingestion_history_newest_last(warehouse):
    """The ingestion panel plots time forward, so rows arrive oldest first."""
    for rows_written in (10, 20, 30):
        warehouse.record_poll(source_url="u", http_status=200, rows_written=rows_written, duration_ms=rows_written)
    polls = analytics.recent_polls(warehouse, limit=10)

    assert [p["rows_written"] for p in polls] == [10, 20, 30]
    assert all("fetched_at" in p and "duration_ms" in p for p in polls)


def test_recent_polls_is_empty_on_a_fresh_warehouse(warehouse):
    assert analytics.recent_polls(warehouse) == []


# --- propagation across service dates --------------------------------------

def _seed_two_runs(warehouse):
    """The same trip on two dates: yesterday badly delayed, today on time."""
    warehouse.execute("INSERT INTO routes (route_id, route_short_name, route_category) VALUES ('r9','ICE 50','ICE')")
    warehouse.execute("INSERT INTO trips (trip_id, route_id, service_id) VALUES ('t9','r9','s9')")
    warehouse.execute(
        "INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES "
        "('S0','Alpha',50.0,8.0), ('S1','Beta',50.5,9.0), ('S2','Gamma',51.0,10.0)"
    )
    base = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=1)
    today = base.date()
    for day, delay in ((today - dt.timedelta(days=1), 14400), (today, 60)):
        for seq, stop in enumerate(("S0", "S1", "S2")):
            warehouse.execute(
                """INSERT INTO stop_time_updates
                   (trip_id, service_date, stop_sequence, feed_timestamp, stop_id,
                    arrival_delay, departure_delay, schedule_relationship, route_category)
                   VALUES ('t9', %s, %s, %s, %s, %s, %s, 'SCHEDULED', 'ICE')""",
                (day, seq, base + dt.timedelta(minutes=seq), stop, delay, delay),
            )


def test_propagation_traces_one_run_not_every_date_interleaved(warehouse):
    """Ordering by stop_sequence alone merges two runs of the same train into a
    sawtooth: seq 0 yesterday, seq 0 today, seq 1 yesterday..."""
    _seed_two_runs(warehouse)

    rows = analytics.delay_propagation(warehouse, "t9")

    assert [r["stop_sequence"] for r in rows] == [0, 1, 2]
    assert {r["arrival_delay"] for r in rows} == {60}, "should trace the latest run only"


def test_propagation_honours_an_explicit_service_date(warehouse):
    _seed_two_runs(warehouse)

    yesterday = (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=1)).date() - dt.timedelta(days=1)
    rows = analytics.delay_propagation(warehouse, "t9", service_date=yesterday)

    assert [r["stop_sequence"] for r in rows] == [0, 1, 2]
    assert {r["arrival_delay"] for r in rows} == {14400}


def test_propagation_of_an_unknown_trip_is_empty(warehouse):
    assert analytics.delay_propagation(warehouse, "no-such-trip") == []
