"""Static GTFS timetable: the reference data the realtime feed points at.

GTFS-RT carries identifiers, not meaning. Joining trip_id and stop_id against
the static timetable is what turns "trip 1536569 delayed 180s at stop 294362"
into "ICE 41 is 3 minutes late at Hanau Hbf" — and it is also how the
long-distance scope (ICE / IC / EC) gets enforced, since the realtime stream
from the open fallback covers all of German public transport.
"""
from __future__ import annotations

import csv
import io
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import requests

# Categories the architecture scopes the project to, plus the EuroCity Express
# variant DB publishes alongside EC.
LONG_DISTANCE_CATEGORIES = frozenset({"ICE", "IC", "EC", "ECE"})


@dataclass(frozen=True)
class Route:
    route_id: str
    route_short_name: str
    route_long_name: str
    category: str
    agency_id: str
    route_type: int | None


@dataclass(frozen=True)
class Stop:
    stop_id: str
    stop_name: str
    stop_lat: float | None
    stop_lon: float | None
    parent_station: str | None


@dataclass(frozen=True)
class Trip:
    trip_id: str
    route_id: str
    service_id: str


def category_from_short_name(short_name: str | None) -> str:
    """"ICE 41" -> "ICE". The feed puts the line number in route_short_name."""
    if not short_name:
        return ""
    return short_name.strip().split()[0] if short_name.strip() else ""


class StaticTimetable:
    """In-memory GTFS reference data.

    The long-distance feed is ~5.6k trips and ~1.2k stops, so holding it in
    dicts is simpler and faster than querying PostgreSQL on the hot path.
    """

    def __init__(self, routes: dict[str, Route], trips: dict[str, Trip], stops: dict[str, Stop]):
        self.routes = routes
        self.trips = trips
        self.stops = stops

    # --- construction ------------------------------------------------------

    @classmethod
    def from_zip(cls, path: Path | str) -> StaticTimetable:
        with zipfile.ZipFile(path) as archive:
            routes = {r.route_id: r for r in _routes(_rows(archive, "routes.txt"))}
            trips = {t.trip_id: t for t in _trips(_rows(archive, "trips.txt"))}
            stops = {s.stop_id: s for s in _stops(_rows(archive, "stops.txt"))}
        return cls(routes, trips, stops)

    # --- lookups -----------------------------------------------------------

    def route_for_trip(self, trip_id: str) -> Route | None:
        trip = self.trips.get(trip_id)
        return self.routes.get(trip.route_id) if trip else None

    def category_for_trip(self, trip_id: str) -> str | None:
        route = self.route_for_trip(trip_id)
        return route.category if route else None

    def stop_name(self, stop_id: str | None) -> str:
        """Never lose the identifier when reference data lags the feed."""
        if not stop_id:
            return ""
        stop = self.stops.get(stop_id)
        return stop.stop_name if stop else stop_id

    def is_long_distance(self, trip_id: str) -> bool:
        return self.category_for_trip(trip_id) in LONG_DISTANCE_CATEGORIES

    def long_distance_trip_ids(self) -> set[str]:
        return {tid for tid in self.trips if self.is_long_distance(tid)}


# --- download --------------------------------------------------------------

STATIC_MAX_AGE_SECONDS = 12 * 3600  # DB regenerates the timetable daily.
DOWNLOAD_TIMEOUT_SECONDS = 120


def download_static(
    settings,
    dest: Path | str,
    session=None,
    max_age_seconds: int = STATIC_MAX_AGE_SECONDS,
) -> Path:
    """Fetch the timetable ZIP, reusing a recent local copy when there is one.

    The timetable changes daily while the realtime feed changes every few
    seconds, so re-downloading it on every collector start is pure waste.
    """
    dest = Path(dest)
    if dest.exists() and (time.time() - dest.stat().st_mtime) < max_age_seconds:
        return dest

    source = settings.static_source()
    session = session or requests.Session()
    response = session.get(source.url, headers=source.headers, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return dest


# --- CSV plumbing ----------------------------------------------------------

def _rows(archive: zipfile.ZipFile, name: str) -> Iterable[dict]:
    """GTFS column order is not fixed, so everything goes through DictReader."""
    with archive.open(name) as handle:
        yield from csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig"))


def _routes(rows: Iterable[dict]) -> Iterable[Route]:
    for row in rows:
        short = (row.get("route_short_name") or "").strip()
        yield Route(
            route_id=row["route_id"],
            route_short_name=short,
            route_long_name=(row.get("route_long_name") or "").strip(),
            category=category_from_short_name(short),
            agency_id=(row.get("agency_id") or "").strip(),
            route_type=_int(row.get("route_type")),
        )


def _trips(rows: Iterable[dict]) -> Iterable[Trip]:
    for row in rows:
        yield Trip(trip_id=row["trip_id"], route_id=row.get("route_id", ""), service_id=row.get("service_id", ""))


def _stops(rows: Iterable[dict]) -> Iterable[Stop]:
    for row in rows:
        yield Stop(
            stop_id=row["stop_id"],
            stop_name=(row.get("stop_name") or "").strip(),
            stop_lat=_float(row.get("stop_lat")),
            stop_lon=_float(row.get("stop_lon")),
            parent_station=(row.get("parent_station") or "").strip() or None,
        )


def _int(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _float(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
