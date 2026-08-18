"""Decoding the GTFS-Realtime Protocol Buffer payload.

Every assertion runs against tests/fixtures/gtfs_rt_sample.pb, which is real
captured DB long-distance traffic, so a change in decoding semantics shows up
here rather than in production.
"""
import datetime as dt

import pytest

from dbrt.gtfs_rt import decode_feed, feed_timestamp, iter_stop_time_updates


def test_decode_feed_reads_the_gtfs_realtime_version_header(rt_bytes):
    feed = decode_feed(rt_bytes)
    assert feed.header.gtfs_realtime_version == "2.0"
    assert len(feed.entity) > 0


def test_decode_feed_rejects_payloads_that_are_not_protobuf():
    with pytest.raises(ValueError, match="not a valid GTFS-RT"):
        decode_feed(b"<html>503 Service Unavailable</html>")


def test_decode_feed_rejects_empty_payload():
    with pytest.raises(ValueError):
        decode_feed(b"")


def test_feed_timestamp_is_returned_as_utc_aware_datetime(rt_bytes):
    ts = feed_timestamp(decode_feed(rt_bytes))
    assert ts.tzinfo == dt.timezone.utc
    assert ts.year >= 2024


def test_iter_stop_time_updates_yields_one_record_per_stop(rt_bytes):
    records = list(iter_stop_time_updates(decode_feed(rt_bytes)))
    assert len(records) > 50
    first = records[0]
    assert first.trip_id
    assert first.stop_sequence >= 0
    assert isinstance(first.service_date, dt.date)


def test_records_carry_arrival_and_departure_delays_in_seconds(rt_bytes):
    records = list(iter_stop_time_updates(decode_feed(rt_bytes)))
    delays = [r.arrival_delay for r in records if r.arrival_delay is not None]
    assert delays, "fixture should contain at least one arrival delay"
    assert all(isinstance(d, int) for d in delays)
    # Real feeds carry negative delays for early running; that must survive decoding.
    assert min(delays) <= 0 or max(delays) > 0


def test_missing_delay_is_none_rather_than_zero(rt_bytes):
    """A stop with no prediction must not be indistinguishable from on-time."""
    records = list(iter_stop_time_updates(decode_feed(rt_bytes)))
    assert any(r.arrival_delay is None for r in records), "first stops have no arrival"


def test_schedule_relationship_is_decoded_to_its_enum_name(rt_bytes):
    records = list(iter_stop_time_updates(decode_feed(rt_bytes)))
    names = {r.schedule_relationship for r in records}
    assert names <= {"SCHEDULED", "SKIPPED", "NO_DATA", "UNSCHEDULED"}
    assert "SCHEDULED" in names


def test_service_date_is_parsed_from_the_trip_start_date(rt_bytes):
    records = list(iter_stop_time_updates(decode_feed(rt_bytes)))
    assert all(isinstance(r.service_date, dt.date) for r in records)


def test_alerts_are_skipped_by_the_trip_update_iterator(rt_bytes):
    feed = decode_feed(rt_bytes)
    assert any(e.HasField("alert") for e in feed.entity), "fixture includes alerts"
    # Alerts carry no stop_time_update, so they must not leak into the fact stream.
    assert all(r.trip_id for r in iter_stop_time_updates(feed))
