"""HTTP and WebSocket surface of the dashboard backend."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from dbrt.api import create_app
from dbrt.gtfs_rt import StopTimeUpdateRecord

pytestmark = pytest.mark.postgres

BASE = dt.datetime(2026, 8, 18, 6, 0, tzinfo=dt.timezone.utc)
DAY = dt.date(2026, 8, 18)


def rec(trip, seq, delay, stop_id, category="ICE", ts=None):
    ts = ts or dt.datetime.now(tz=dt.timezone.utc)
    return (
        StopTimeUpdateRecord(
            trip_id=trip, service_date=DAY, stop_sequence=seq, stop_id=stop_id,
            arrival_delay=delay, departure_delay=delay, arrival_time=ts, departure_time=ts,
            schedule_relationship="SCHEDULED", trip_schedule_relationship="SCHEDULED", route_id="1",
        ),
        ts, category,
    )


@pytest.fixture()
def client(warehouse, pg_settings):
    warehouse.insert_stop_time_updates([
        rec("A", 0, 0, "S1"), rec("A", 1, 240, "S2"), rec("A", 2, 900, "S3"),
        rec("B", 0, 60, "S1", category="IC"), rec("B", 1, 30, "S2", category="IC"),
    ])
    with warehouse.conn.cursor() as cur:
        cur.execute("INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES "
                    "('S1','Berlin Hbf',52.525,13.369),('S2','Hannover Hbf',52.377,9.741),"
                    "('S3','Köln Hbf',50.943,6.958)")
    warehouse.record_poll(feed_timestamp=BASE, source_url="test", http_status=200,
                          entity_count=5, long_distance_trips=2, rows_written=5, duration_ms=12)
    with TestClient(create_app(pg_settings)) as c:
        yield c


def test_health_reports_ok_and_database_connectivity(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["database"] is True


def test_summary_exposes_punctuality_and_data_lineage(client):
    body = client.get("/api/summary").json()
    assert body["punctuality"]["total_stops"] == 5
    assert body["source"]["label"]
    assert body["source"]["official"] in (True, False)
    assert "threshold_seconds" in body["punctuality"]


def test_summary_includes_the_latest_ingestion_state(client):
    body = client.get("/api/summary").json()
    assert body["feed"]["last_poll"] is not None
    assert body["feed"]["rows_written"] == 5


def test_timeseries_returns_chronological_buckets(client):
    rows = client.get("/api/timeseries?bucket=5&hours=24").json()
    assert isinstance(rows, list) and rows
    assert [r["bucket"] for r in rows] == sorted(r["bucket"] for r in rows)


def test_timeseries_rejects_an_out_of_range_bucket(client):
    assert client.get("/api/timeseries?bucket=0").status_code == 422
    assert client.get("/api/timeseries?bucket=9999").status_code == 422


def test_stations_are_ranked_and_named(client):
    rows = client.get("/api/stations?limit=5&min_observations=1").json()
    assert rows[0]["stop_name"] == "Köln Hbf"
    assert rows[0]["mean_delay_seconds"] >= rows[-1]["mean_delay_seconds"]


def test_categories_split_ice_and_ic(client):
    rows = client.get("/api/categories").json()
    assert {r["route_category"] for r in rows} == {"ICE", "IC"}


def test_distribution_bands_sum_to_the_observation_count(client):
    rows = client.get("/api/distribution").json()
    assert sum(r["stops"] for r in rows) == 5


def test_network_snapshot_feeds_the_map(client):
    rows = client.get("/api/network").json()
    assert {r["trip_id"] for r in rows} == {"A", "B"}
    assert all("stop_lat" in r for r in rows)


def test_trip_propagation_returns_the_route_in_order(client):
    rows = client.get("/api/trips/A/propagation").json()
    assert [r["stop_sequence"] for r in rows] == [0, 1, 2]


def test_unknown_trip_propagation_is_404(client):
    assert client.get("/api/trips/NOPE/propagation").status_code == 404


def test_worst_trips_are_ordered_by_peak_delay(client):
    rows = client.get("/api/trips/worst?limit=5").json()
    assert rows[0]["trip_id"] == "A"


def test_model_endpoint_reports_untrained_state_without_erroring(client):
    body = client.get("/api/model").json()
    assert "trained" in body


def test_dashboard_html_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_websocket_pushes_a_full_dashboard_payload(client):
    with client.websocket_connect("/ws") as ws:
        payload = ws.receive_json()
    assert {"summary", "timeseries", "stations", "categories", "network", "distribution"} <= set(payload)
    assert payload["summary"]["punctuality"]["total_stops"] == 5


def test_websocket_payload_carries_a_server_timestamp(client):
    with client.websocket_connect("/ws") as ws:
        payload = ws.receive_json()
    assert dt.datetime.fromisoformat(payload["generated_at"])


def test_station_geometry_endpoint_returns_coordinates_for_the_map_backdrop(client):
    rows = client.get("/api/stations/geo").json()
    assert len(rows) == 3
    assert {"stop_lat", "stop_lon", "stop_name"} <= set(rows[0])
    assert all(r["stop_lat"] is not None for r in rows)


def test_station_geometry_excludes_stops_without_coordinates(warehouse, pg_settings):
    with warehouse.conn.cursor() as cur:
        cur.execute("INSERT INTO stops (stop_id, stop_name) VALUES ('X','No Coords')")
        cur.execute("INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) "
                    "VALUES ('Y','Located',52.5,13.4)")
    with TestClient(create_app(pg_settings)) as c:
        names = [r["stop_name"] for r in c.get("/api/stations/geo").json()]
    assert names == ["Located"]
