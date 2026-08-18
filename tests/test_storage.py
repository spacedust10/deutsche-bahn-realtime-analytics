"""PostgreSQL persistence: reference data, the fact table, and idempotency."""
import datetime as dt

import pytest

from dbrt.gtfs_rt import StopTimeUpdateRecord
from dbrt.static_gtfs import StaticTimetable

pytestmark = pytest.mark.postgres

FEED_TS = dt.datetime(2026, 8, 18, 6, 0, tzinfo=dt.timezone.utc)
SERVICE_DATE = dt.date(2026, 8, 18)


def record(trip_id="1536569", seq=0, arr=60, dep=120, ts=FEED_TS, stop_id="540068", category="ICE"):
    return StopTimeUpdateRecord(
        trip_id=trip_id,
        service_date=SERVICE_DATE,
        stop_sequence=seq,
        stop_id=stop_id,
        arrival_delay=arr,
        departure_delay=dep,
        arrival_time=ts,
        departure_time=ts,
        schedule_relationship="SCHEDULED",
        trip_schedule_relationship="SCHEDULED",
        route_id="1",
    ), ts, category


def write(warehouse, *records):
    return warehouse.insert_stop_time_updates([(r, ts, cat) for r, ts, cat in records])


def test_apply_schema_is_idempotent(warehouse):
    warehouse.apply_schema()
    warehouse.apply_schema()
    assert warehouse.count("stop_time_updates") == 0


def test_insert_writes_rows_and_returns_the_count(warehouse):
    written = write(warehouse, record(seq=0), record(seq=1))
    assert written == 2
    assert warehouse.count("stop_time_updates") == 2


def test_reinserting_the_same_feed_timestamp_is_a_no_op(warehouse):
    """Restarting the collector must not duplicate history."""
    write(warehouse, record(seq=0))
    written = write(warehouse, record(seq=0))
    assert written == 0
    assert warehouse.count("stop_time_updates") == 1


def test_a_later_feed_timestamp_appends_a_new_observation(warehouse):
    write(warehouse, record(seq=0, arr=60))
    later = FEED_TS + dt.timedelta(minutes=5)
    write(warehouse, record(seq=0, arr=300, ts=later))
    assert warehouse.count("stop_time_updates") == 2


def test_empty_batch_writes_nothing_and_does_not_error(warehouse):
    assert warehouse.insert_stop_time_updates([]) == 0


def test_negative_delays_round_trip_unchanged(warehouse):
    write(warehouse, record(seq=0, arr=-180, dep=-120))
    row = warehouse.fetchone("SELECT arrival_delay, departure_delay FROM stop_time_updates")
    assert row == (-180, -120)


def test_null_delay_is_stored_as_null_not_zero(warehouse):
    write(warehouse, record(seq=0, arr=None, dep=None))
    row = warehouse.fetchone("SELECT arrival_delay, departure_delay FROM stop_time_updates")
    assert row == (None, None)


def test_route_category_is_persisted_for_filtering(warehouse):
    write(warehouse, record(category="ICE"))
    assert warehouse.fetchone("SELECT route_category FROM stop_time_updates")[0] == "ICE"


def test_reference_data_loads_from_a_static_timetable(warehouse, static_zip):
    timetable = StaticTimetable.from_zip(static_zip)
    warehouse.load_timetable(timetable)
    assert warehouse.count("routes") == len(timetable.routes)
    assert warehouse.count("stops") == len(timetable.stops)
    assert warehouse.count("trips") == len(timetable.trips)


def test_reloading_the_timetable_updates_rather_than_duplicating(warehouse, static_zip):
    timetable = StaticTimetable.from_zip(static_zip)
    warehouse.load_timetable(timetable)
    warehouse.load_timetable(timetable)
    assert warehouse.count("routes") == len(timetable.routes)


def test_current_stop_delays_view_returns_only_the_latest_observation(warehouse):
    write(warehouse, record(seq=0, arr=60))
    write(warehouse, record(seq=0, arr=900, ts=FEED_TS + dt.timedelta(minutes=10)))
    rows = warehouse.fetchall("SELECT arrival_delay FROM current_stop_delays")
    assert rows == [(900,)]


def test_record_poll_writes_an_audit_row(warehouse):
    warehouse.record_poll(
        feed_timestamp=FEED_TS, source_url="https://example/feed.pb", http_status=200,
        payload_bytes=1234, entity_count=10, long_distance_trips=3, rows_written=7, duration_ms=42,
    )
    assert warehouse.count("feed_polls") == 1


def test_record_poll_stores_the_error_message_on_failure(warehouse):
    warehouse.record_poll(source_url="u", http_status=503, error="upstream unavailable")
    assert warehouse.fetchone("SELECT error FROM feed_polls")[0] == "upstream unavailable"


def test_latest_feed_timestamp_is_none_on_an_empty_warehouse(warehouse):
    assert warehouse.latest_feed_timestamp() is None


def test_latest_feed_timestamp_reflects_the_newest_observation(warehouse):
    write(warehouse, record(seq=0))
    newest = FEED_TS + dt.timedelta(minutes=30)
    write(warehouse, record(seq=1, ts=newest))
    assert warehouse.latest_feed_timestamp() == newest
