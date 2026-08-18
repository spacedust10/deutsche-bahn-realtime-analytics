"""One poll cycle: fetch -> decode -> scope to long distance -> persist -> audit."""
import datetime as dt

import pytest

from dbrt.collector import PollSummary, collect_once
from dbrt.feed_client import FeedResult
from dbrt.static_gtfs import StaticTimetable


class FakeClient:
    def __init__(self, *results):
        self.results = list(results)

    def fetch(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeWarehouse:
    def __init__(self):
        self.batches = []
        self.polls = []

    def insert_stop_time_updates(self, batch):
        self.batches.append(batch)
        return len(batch)

    def record_poll(self, **kwargs):
        self.polls.append(kwargs)


def ok_result(payload):
    return FeedResult(payload, 200, False, "gtfs.de long-distance (open)", False, len(payload))


@pytest.fixture()
def timetable(static_zip):
    return StaticTimetable.from_zip(static_zip)


def test_collect_once_persists_long_distance_stop_updates(rt_bytes, timetable):
    warehouse = FakeWarehouse()
    summary = collect_once(FakeClient(ok_result(rt_bytes)), timetable, warehouse)

    assert isinstance(summary, PollSummary)
    assert summary.rows_written > 0
    assert warehouse.batches and len(warehouse.batches[0]) == summary.rows_written


def test_every_persisted_row_belongs_to_a_long_distance_train(rt_bytes, timetable):
    """The open fallback carries all of German transit; scope must be enforced."""
    warehouse = FakeWarehouse()
    collect_once(FakeClient(ok_result(rt_bytes)), timetable, warehouse)

    categories = {category for _, _, category in warehouse.batches[0]}
    assert categories <= {"ICE", "IC", "EC", "ECE"}
    assert categories


def test_each_row_carries_the_feed_timestamp_not_wall_clock(rt_bytes, timetable):
    warehouse = FakeWarehouse()
    summary = collect_once(FakeClient(ok_result(rt_bytes)), timetable, warehouse)
    stamps = {feed_ts for _, feed_ts, _ in warehouse.batches[0]}
    assert stamps == {summary.feed_timestamp}
    assert summary.feed_timestamp.tzinfo is dt.timezone.utc


def test_summary_counts_distinct_long_distance_trips(rt_bytes, timetable):
    summary = collect_once(FakeClient(ok_result(rt_bytes)), timetable, FakeWarehouse())
    assert 0 < summary.long_distance_trips <= summary.entity_count


def test_a_poll_is_always_audited(rt_bytes, timetable):
    warehouse = FakeWarehouse()
    collect_once(FakeClient(ok_result(rt_bytes)), timetable, warehouse)
    assert len(warehouse.polls) == 1
    assert warehouse.polls[0]["http_status"] == 200
    assert warehouse.polls[0]["error"] is None


def test_not_modified_short_circuits_without_writing(timetable):
    warehouse = FakeWarehouse()
    unchanged = FeedResult(None, 304, True, "open", False, 0)

    summary = collect_once(FakeClient(unchanged), timetable, warehouse)

    assert summary.not_modified is True
    assert summary.rows_written == 0
    assert warehouse.batches == []
    assert warehouse.polls[0]["http_status"] == 304


def test_transport_failure_is_captured_in_the_summary_not_raised(timetable):
    warehouse = FakeWarehouse()
    summary = collect_once(FakeClient(RuntimeError("HTTP 503")), timetable, warehouse)

    assert summary.error == "HTTP 503"
    assert summary.rows_written == 0
    assert warehouse.polls[0]["error"] == "HTTP 503"


def test_a_corrupt_payload_is_reported_rather_than_crashing_the_loop(timetable):
    warehouse = FakeWarehouse()
    summary = collect_once(FakeClient(ok_result(b"<html>maintenance</html>")), timetable, warehouse)

    assert summary.error is not None
    assert "GTFS-RT" in summary.error
    assert warehouse.batches == []


def test_duration_is_measured_for_every_poll(rt_bytes, timetable):
    summary = collect_once(FakeClient(ok_result(rt_bytes)), timetable, FakeWarehouse())
    assert summary.duration_ms >= 0
