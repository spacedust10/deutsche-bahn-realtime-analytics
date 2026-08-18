"""Contract tests against the live upstream feeds.

These are the only tests that touch the network. They exist to catch upstream
changes — a moved endpoint, a new GTFS-RT version, a renamed column in the
timetable — that unit tests running on captured bytes cannot see. Excluded from
CI (`-m "not network"`) so a Deutsche Bahn outage never fails a pull request.

Run explicitly:  pytest -m network
"""
import pytest
import requests

from dbrt.config import Settings
from dbrt.feed_client import FeedClient
from dbrt.gtfs_rt import decode_feed, feed_timestamp, iter_stop_time_updates

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live_feed():
    settings = Settings.from_env({})  # Open fallback: no credentials needed.
    try:
        result = FeedClient(settings).fetch()
    except requests.RequestException as exc:
        pytest.skip(f"live feed unreachable: {exc}")
    return decode_feed(result.payload)


def test_official_db_endpoint_exists_and_requires_a_key():
    """The architecture's primary source must still be there and still gated."""
    url = Settings.from_env({}).db_gtfs_rt_url
    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        pytest.skip(f"DB endpoint unreachable: {exc}")
    assert response.status_code in (401, 403), (
        f"expected the official feed to require DB-Api-Key, got {response.status_code}"
    )


def test_live_feed_is_gtfs_realtime_version_two(live_feed):
    assert live_feed.header.gtfs_realtime_version.startswith("2")


def test_live_feed_carries_trip_updates(live_feed):
    assert any(e.HasField("trip_update") for e in live_feed.entity)


def test_live_feed_timestamp_is_recent(live_feed):
    import datetime as dt

    age = dt.datetime.now(tz=dt.timezone.utc) - feed_timestamp(live_feed)
    assert age < dt.timedelta(hours=1), f"feed is stale by {age}"


def test_live_feed_decodes_into_usable_records(live_feed):
    records = list(iter_stop_time_updates(live_feed))
    assert len(records) > 1000
    assert any(r.arrival_delay is not None for r in records)


def test_static_timetable_still_exposes_the_columns_we_join_on(tmp_path):
    from dbrt.static_gtfs import StaticTimetable, download_static

    settings = Settings.from_env({})
    try:
        path = download_static(settings, tmp_path / "gtfs.zip", max_age_seconds=0)
    except requests.RequestException as exc:
        pytest.skip(f"timetable unreachable: {exc}")

    timetable = StaticTimetable.from_zip(path)
    assert len(timetable.trips) > 1000
    assert len(timetable.stops) > 500
    assert timetable.long_distance_trip_ids(), "no ICE/IC/EC trips found in the timetable"


def test_live_trip_ids_still_join_against_the_static_timetable(live_feed, tmp_path):
    """The join is the whole pipeline: without it there is no long-distance scope."""
    from dbrt.static_gtfs import StaticTimetable, download_static

    settings = Settings.from_env({})
    try:
        path = download_static(settings, tmp_path / "gtfs.zip", max_age_seconds=0)
    except requests.RequestException as exc:
        pytest.skip(f"timetable unreachable: {exc}")

    timetable = StaticTimetable.from_zip(path)
    matched = {
        r.trip_id for r in iter_stop_time_updates(live_feed)
        if timetable.is_long_distance(r.trip_id)
    }
    assert matched, "no live trip_id matched the long-distance timetable"
