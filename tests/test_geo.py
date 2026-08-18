"""Rail network geometry served as GeoJSON, ready for MapLibre."""
import datetime as dt

import pytest

from dbrt import analytics

pytestmark = pytest.mark.postgres


def _seed_line(warehouse):
    warehouse.execute("INSERT INTO routes (route_id, route_short_name, route_category) VALUES ('r1','ICE 1','ICE')")
    warehouse.execute("INSERT INTO trips (trip_id, route_id, service_id) VALUES ('t1','r1','s1'), ('t2','r1','s1')")
    warehouse.execute(
        "INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon) VALUES "
        "('A','Alpha',50.0,8.0), ('B','Beta',50.5,9.0), ('C','Gamma',51.0,10.0)"
    )
    warehouse.execute(
        "INSERT INTO stop_times (trip_id, stop_sequence, stop_id, arrival_seconds, departure_seconds) VALUES "
        "('t1',0,'A',36000,36000), ('t1',1,'B',37800,37800), ('t1',2,'C',39600,39600),"
        "('t2',0,'A',50000,50000), ('t2',1,'B',51800,51800)"   # shares the A-B link
    )


def test_network_geometry_is_a_geojson_feature_collection(warehouse):
    _seed_line(warehouse)
    geo = analytics.network_geometry(warehouse)

    assert geo["type"] == "FeatureCollection"
    assert isinstance(geo["features"], list)


def test_each_feature_is_a_linestring_between_two_stations(warehouse):
    _seed_line(warehouse)
    feature = analytics.network_geometry(warehouse)["features"][0]

    assert feature["geometry"]["type"] == "LineString"
    coords = feature["geometry"]["coordinates"]
    assert len(coords) == 2
    # GeoJSON is [lon, lat] — the reverse of how the rows are stored.
    for lon, lat in coords:
        assert 5 < lon < 16 and 45 < lat < 56


def test_shared_track_appears_once_not_once_per_trip(warehouse):
    """Two trips share A-B; the map needs that link drawn a single time."""
    _seed_line(warehouse)
    features = analytics.network_geometry(warehouse)["features"]
    assert len(features) == 2  # A-B and B-C


def test_stations_are_returned_as_points_with_names(warehouse):
    _seed_line(warehouse)
    stations = analytics.station_points(warehouse)

    assert stations["type"] == "FeatureCollection"
    names = {f["properties"]["name"] for f in stations["features"]}
    assert {"Alpha", "Beta", "Gamma"} <= names
    for feature in stations["features"]:
        assert feature["geometry"]["type"] == "Point"


def test_stations_without_coordinates_are_omitted(warehouse):
    _seed_line(warehouse)
    warehouse.execute("INSERT INTO stops (stop_id, stop_name) VALUES ('D','Nowhere')")
    names = {f["properties"]["name"] for f in analytics.station_points(warehouse)["features"]}
    assert "Nowhere" not in names


def test_empty_warehouse_yields_empty_collections(warehouse):
    assert analytics.network_geometry(warehouse)["features"] == []
    assert analytics.station_points(warehouse)["features"] == []
