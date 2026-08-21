"""Static timetable loading and long-distance classification.

Static GTFS is what turns opaque realtime identifiers into meaning: trip_id ->
train category, stop_id -> station name and coordinates.
"""
import pytest

from dbrt.static_gtfs import (
    LONG_DISTANCE_CATEGORIES,
    StaticTimetable,
    category_from_short_name,
)


@pytest.fixture(scope="module")
def timetable(static_zip):
    return StaticTimetable.from_zip(static_zip)


# --- category parsing ------------------------------------------------------

@pytest.mark.parametrize(
    "short_name,expected",
    [
        ("ICE", "ICE"),
        ("ICE 11", "ICE"),      # Real feed encodes the line number in the name.
        ("IC 35", "IC"),
        ("EC 62", "EC"),
        ("ECE 20", "ECE"),
        ("EN", "EN"),
        ("RJ", "RJ"),
        ("  ICE  42 ", "ICE"),
        ("", ""),
    ],
)
def test_category_is_the_leading_token_of_the_route_short_name(short_name, expected):
    assert category_from_short_name(short_name) == expected


def test_category_of_none_is_empty_not_an_exception():
    assert category_from_short_name(None) == ""


def test_long_distance_set_covers_the_architecture_scope():
    assert {"ICE", "IC", "EC"} <= LONG_DISTANCE_CATEGORIES


# --- loading ---------------------------------------------------------------

def test_from_zip_loads_routes_trips_and_stops(timetable):
    assert len(timetable.routes) > 0
    assert len(timetable.trips) > 0
    assert len(timetable.stops) > 0


def test_trip_ids_resolve_to_a_train_category(timetable):
    trip_id = next(iter(timetable.trips))
    assert timetable.category_for_trip(trip_id) in LONG_DISTANCE_CATEGORIES | {"EN", "RJ", "ECE", ""}


def test_unknown_trip_id_resolves_to_none_not_a_keyerror(timetable):
    assert timetable.category_for_trip("no-such-trip") is None


def test_stop_names_resolve_from_stop_id(timetable):
    stop_id, stop = next(iter(timetable.stops.items()))
    assert timetable.stop_name(stop_id) == stop.stop_name
    assert stop.stop_name


def test_unknown_stop_id_falls_back_to_the_raw_id(timetable):
    """Station reference data lags the realtime feed; never lose the identifier."""
    assert timetable.stop_name("999999999") == "999999999"


def test_stop_coordinates_are_floats_within_central_europe(timetable):
    located = [s for s in timetable.stops.values() if s.stop_lat is not None]
    assert located
    assert all(45 < s.stop_lat < 58 for s in located)
    assert all(2 < s.stop_lon < 25 for s in located)


def test_long_distance_trip_ids_is_a_nonempty_subset_of_all_trips(timetable):
    ld = timetable.long_distance_trip_ids()
    assert ld
    assert ld <= set(timetable.trips)


def test_is_long_distance_matches_the_category_of_the_trip(timetable):
    ld = timetable.long_distance_trip_ids()
    sample = next(iter(ld))
    assert timetable.is_long_distance(sample) is True
    assert timetable.is_long_distance("no-such-trip") is False


def test_route_for_trip_exposes_the_short_name_for_labelling(timetable):
    trip_id = next(iter(timetable.long_distance_trip_ids()))
    route = timetable.route_for_trip(trip_id)
    assert route is not None
    assert route.route_short_name


# --- download / caching ----------------------------------------------------
