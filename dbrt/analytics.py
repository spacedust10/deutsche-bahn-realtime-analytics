"""SQL analytics over the realtime history.

Punctuality follows Deutsche Bahn's own published definition: a stop is
punctual when it is less than 6 minutes late. Using DB's threshold rather than
inventing one keeps the numbers comparable to DB's published statistics.

Every query reads `current_stop_delays` (the latest observation per stop) for
"state of the network right now" questions, and the raw fact table when the
question is about how delays evolved.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

from . import static_gtfs

PUNCTUALITY_THRESHOLD_SECONDS = 360  # DB: "pünktlich" = under 6 minutes late.

# Delay is measured on arrival where the feed provides it, otherwise departure;
# the first stop of a trip has no arrival, the last has no departure.
_DELAY = "COALESCE(arrival_delay, departure_delay)"


def punctuality(warehouse, hours: int = 24) -> dict[str, Any]:
    """Headline punctuality across the whole observed network."""
    row = warehouse.fetchone(
        f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE {_DELAY} < %s) AS punctual,
               avg({_DELAY})::float,
               max({_DELAY}),
               count(DISTINCT trip_id)
        FROM   current_stop_delays
        WHERE  {_DELAY} IS NOT NULL
          AND  feed_timestamp > now() - make_interval(hours => %s)
        """,
        (PUNCTUALITY_THRESHOLD_SECONDS, hours),
    )
    total, punctual, mean, worst, trips = row or (0, 0, None, None, 0)
    return {
        "total_stops": total or 0,
        "punctual_stops": punctual or 0,
        "punctuality_pct": round(100 * punctual / total, 2) if total else 0.0,
        "mean_delay_seconds": round(mean, 2) if mean is not None else 0.0,
        "max_delay_seconds": worst or 0,
        "trips": trips or 0,
        "threshold_seconds": PUNCTUALITY_THRESHOLD_SECONDS,
    }


def station_delays(warehouse, limit: int = 15, min_observations: int = 5, hours: int = 24) -> list[dict]:
    """Worst stations by mean delay.

    `min_observations` exists because ranking a station on one observation
    produces noise: a single very late train would top the chart.
    """
    # GTFS models every platform as its own stop_id under a parent station, so
    # observations are rolled up to the parent before ranking. Without this a
    # single city appears several times with a slice of its traffic each.
    rows = warehouse.fetchall(
        f"""
        SELECT COALESCE(s.stop_name, d.stop_id) AS stop_name,
               COALESCE(s.parent_station, d.stop_id) AS station_id,
               max(s.stop_lat) AS stop_lat, max(s.stop_lon) AS stop_lon,
               count(*) AS observations,
               avg({_DELAY})::float AS mean_delay,
               max({_DELAY}) AS max_delay,
               100.0 * count(*) FILTER (WHERE {_DELAY} < %s) / count(*) AS punctuality
        FROM   current_stop_delays d
        LEFT   JOIN stops s ON s.stop_id = d.stop_id
        WHERE  {_DELAY} IS NOT NULL
          AND  d.feed_timestamp > now() - make_interval(hours => %s)
        GROUP  BY 1, 2
        HAVING count(*) >= %s
        ORDER  BY mean_delay DESC
        LIMIT  %s
        """,
        (PUNCTUALITY_THRESHOLD_SECONDS, hours, min_observations, limit),
    )
    return [
        {
            "stop_name": r[0], "stop_id": r[1], "stop_lat": r[2], "stop_lon": r[3],
            "observations": r[4], "mean_delay_seconds": round(r[5], 1),
            "max_delay_seconds": r[6], "punctuality_pct": round(float(r[7]), 1),
        }
        for r in rows
    ]


def category_breakdown(warehouse, hours: int = 24) -> list[dict]:
    """Punctuality per train category (ICE / IC / EC / ECE)."""
    rows = warehouse.fetchall(
        f"""
        SELECT route_category,
               count(DISTINCT trip_id) AS trips,
               count(*) AS stops,
               avg({_DELAY})::float AS mean_delay,
               100.0 * count(*) FILTER (WHERE {_DELAY} < %s) / count(*) AS punctuality
        FROM   current_stop_delays
        WHERE  {_DELAY} IS NOT NULL AND route_category IS NOT NULL
          AND  feed_timestamp > now() - make_interval(hours => %s)
        GROUP  BY 1
        ORDER  BY stops DESC
        """,
        (PUNCTUALITY_THRESHOLD_SECONDS, hours),
    )
    return [
        {"route_category": r[0], "trips": r[1], "stops": r[2],
         "mean_delay_seconds": round(r[3], 1), "punctuality_pct": round(float(r[4]), 1)}
        for r in rows
    ]


def delay_propagation(warehouse, trip_id: str, service_date: dt.date | None = None) -> list[dict]:
    """How one train's delay evolves stop by stop along its route."""
    rows = warehouse.fetchall(
        """
        SELECT d.stop_sequence, d.stop_id, COALESCE(s.stop_name, d.stop_id),
               d.arrival_delay, d.departure_delay, s.stop_lat, s.stop_lon, d.route_category
        FROM   current_stop_delays d
        LEFT   JOIN stops s ON s.stop_id = d.stop_id
        WHERE  d.trip_id = %s AND (%s::date IS NULL OR d.service_date = %s)
        ORDER  BY d.stop_sequence
        """,
        (trip_id, service_date, service_date),
    )
    return [
        {"stop_sequence": r[0], "stop_id": r[1], "stop_name": r[2],
         "arrival_delay": r[3], "departure_delay": r[4],
         "stop_lat": r[5], "stop_lon": r[6], "route_category": r[7]}
        for r in rows
    ]


def worst_trips(warehouse, limit: int = 10, hours: int = 24) -> list[dict]:
    rows = warehouse.fetchall(
        f"""
        SELECT d.trip_id, d.service_date, max(d.route_category) AS category,
               max({_DELAY}) AS max_delay, avg({_DELAY})::float AS mean_delay,
               count(*) AS stops, max(r.route_short_name) AS route_name
        FROM   current_stop_delays d
        LEFT   JOIN trips t ON t.trip_id = d.trip_id
        LEFT   JOIN routes r ON r.route_id = t.route_id
        WHERE  {_DELAY} IS NOT NULL
          AND  d.feed_timestamp > now() - make_interval(hours => %s)
        GROUP  BY d.trip_id, d.service_date
        ORDER  BY max_delay DESC
        LIMIT  %s
        """,
        (hours, limit),
    )
    return [
        {"trip_id": r[0], "service_date": r[1].isoformat(), "route_category": r[2],
         "max_delay_seconds": r[3], "mean_delay_seconds": round(r[4], 1),
         "stops": r[5], "route_name": r[6]}
        for r in rows
    ]


def delay_timeseries(warehouse, bucket_minutes: int = 5, hours: int = 6) -> list[dict]:
    """Mean delay and punctuality per time bucket — the dashboard's main chart.

    Reads the raw fact table rather than the latest-state view: the point is
    how the network moved over time, which the view deliberately collapses.
    """
    rows = warehouse.fetchall(
        f"""
        SELECT to_timestamp(floor(extract(epoch FROM feed_timestamp) / (%s * 60)) * (%s * 60)) AS bucket,
               count(*) AS stops,
               count(DISTINCT trip_id) AS trips,
               avg({_DELAY})::float AS mean_delay,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY {_DELAY})::float AS p90_delay,
               100.0 * count(*) FILTER (WHERE {_DELAY} < %s) / count(*) AS punctuality
        FROM   stop_time_updates
        WHERE  {_DELAY} IS NOT NULL
          AND  feed_timestamp > now() - make_interval(hours => %s)
        GROUP  BY bucket
        ORDER  BY bucket
        """,
        (bucket_minutes, bucket_minutes, PUNCTUALITY_THRESHOLD_SECONDS, hours),
    )
    return [
        {"bucket": r[0].isoformat(), "stops": r[1], "trips": r[2],
         "mean_delay_seconds": round(r[3], 1), "p90_delay_seconds": round(r[4], 1),
         "punctuality_pct": round(float(r[5]), 1)}
        for r in rows
    ]


def network_snapshot(warehouse, limit: int = 400) -> list[dict]:
    """Current position-ish state of every tracked train, for the live map.

    "Position" is the last stop with a reported delay — GTFS-RT TripUpdates
    carry no coordinates, so the station is the best available locator.
    """
    rows = warehouse.fetchall(
        f"""
        SELECT DISTINCT ON (d.trip_id)
               d.trip_id, d.route_category, COALESCE(s.stop_name, d.stop_id) AS stop_name,
               s.stop_lat, s.stop_lon, {_DELAY} AS delay, d.stop_sequence,
               d.feed_timestamp, r.route_short_name
        FROM   current_stop_delays d
        LEFT   JOIN stops s ON s.stop_id = d.stop_id
        LEFT   JOIN trips t ON t.trip_id = d.trip_id
        LEFT   JOIN routes r ON r.route_id = t.route_id
        WHERE  {_DELAY} IS NOT NULL
        ORDER  BY d.trip_id, d.stop_sequence DESC
        LIMIT  %s
        """,
        (limit,),
    )
    return [
        {"trip_id": r[0], "route_category": r[1], "stop_name": r[2],
         "stop_lat": r[3], "stop_lon": r[4], "current_delay_seconds": r[5],
         "stop_sequence": r[6], "observed_at": r[7].isoformat(), "route_name": r[8]}
        for r in rows
    ]


def delay_distribution(warehouse, hours: int = 24) -> list[dict]:
    """Histogram of delays in the bands a passenger actually feels."""
    rows = warehouse.fetchall(
        f"""
        SELECT band, count(*) FROM (
            SELECT CASE
                     WHEN {_DELAY} < 0    THEN '5:early'
                     WHEN {_DELAY} < 360  THEN '0:on time (<6 min)'
                     WHEN {_DELAY} < 900  THEN '1:6-15 min'
                     WHEN {_DELAY} < 1800 THEN '2:15-30 min'
                     WHEN {_DELAY} < 3600 THEN '3:30-60 min'
                     ELSE '4:60+ min'
                   END AS band
            FROM   current_stop_delays
            WHERE  {_DELAY} IS NOT NULL
              AND  feed_timestamp > now() - make_interval(hours => %s)
        ) t
        GROUP BY band ORDER BY band
        """,
        (hours,),
    )
    # The numeric prefix only exists to force sort order; strip it for display.
    return [{"band": r[0].split(":", 1)[1], "stops": r[1]} for r in rows]


def cancellations(warehouse, hours: int = 24) -> dict[str, Any]:
    """Skipped stops and cancelled trips.

    GTFS-RT expresses a dropped station as schedule_relationship=SKIPPED on the
    stop, and a cancelled service as CANCELED on the trip. They are different
    events for a passenger, so they are counted separately.
    """
    row = warehouse.fetchone(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE c.schedule_relationship = 'SKIPPED') AS skipped,
               count(DISTINCT c.trip_id) FILTER (WHERE c.schedule_relationship = 'SKIPPED') AS affected,
               count(DISTINCT c.trip_id) FILTER (WHERE u.trip_schedule_relationship = 'CANCELED') AS cancelled
        FROM   current_stop_delays c
        -- The latest-state view carries the stop relationship but not the trip
        -- one, so the fact table is joined back for trip-level cancellation.
        JOIN   stop_time_updates u
          ON   u.trip_id = c.trip_id AND u.service_date = c.service_date
         AND   u.stop_sequence = c.stop_sequence AND u.feed_timestamp = c.feed_timestamp
        WHERE  c.feed_timestamp > now() - make_interval(hours => %s)
        """,
        (hours,),
    )
    total, skipped, affected, cancelled = row or (0, 0, 0, 0)
    return {
        "skipped_stops": skipped or 0,
        "affected_trips": affected or 0,
        "cancelled_trips": cancelled or 0,
        "total_stops": total or 0,
        "skipped_pct": round(100 * skipped / total, 2) if total else 0.0,
    }


def skipped_stations(warehouse, limit: int = 10, hours: int = 24) -> list[dict]:
    """Stations most often dropped from a route."""
    rows = warehouse.fetchall(
        """
        SELECT COALESCE(s.stop_name, u.stop_id) AS stop_name, u.stop_id, count(*) AS skipped
        FROM   stop_time_updates u
        LEFT   JOIN stops s ON s.stop_id = u.stop_id
        WHERE  u.schedule_relationship = 'SKIPPED'
          AND  u.feed_timestamp > now() - make_interval(hours => %s)
        GROUP  BY 1, 2
        ORDER  BY skipped DESC, stop_name
        LIMIT  %s
        """,
        (hours, limit),
    )
    return [{"stop_name": r[0], "stop_id": r[1], "skipped": r[2]} for r in rows]


def station_geometry(warehouse) -> list[dict]:
    """Every located station, for the map backdrop.

    Static reference data: the dashboard fetches it once and reuses it, rather
    than shipping 1.2k coordinates in every realtime push.
    """
    rows = warehouse.fetchall(
        """SELECT stop_id, stop_name, stop_lat, stop_lon
           FROM   stops
           WHERE  stop_lat IS NOT NULL AND stop_lon IS NOT NULL
           ORDER  BY stop_name"""
    )
    return [{"stop_id": r[0], "stop_name": r[1], "stop_lat": r[2], "stop_lon": r[3]} for r in rows]


# ---------------------------------------------------------------------------
# Live map geometry
# ---------------------------------------------------------------------------

def _interpolate(frm: tuple[float, float], to: tuple[float, float], progress: float) -> tuple[float, float]:
    """Linear interpolation between two stations.

    ponytail: straight-line, not great-circle. Over German inter-station spans
    (tens of km) the difference is under a pixel at dashboard zoom levels.
    """
    return (frm[0] + (to[0] - frm[0]) * progress, frm[1] + (to[1] - frm[1]) * progress)


def _bearing(frm: tuple[float, float], to: tuple[float, float]) -> float:
    """Compass bearing in degrees, so map markers can point where they travel."""
    lat1, lon1, lat2, lon2 = map(math.radians, (frm[0], frm[1], to[0], to[1]))
    d_lon = lon2 - lon1
    y = math.sin(d_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def history_window(warehouse) -> dict[str, Any]:
    """Time span the collected history covers — the range of the map's slider."""
    row = warehouse.fetchone("SELECT MIN(feed_timestamp), MAX(feed_timestamp) FROM stop_time_updates")
    start, end = (row or (None, None))
    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }


def live_positions(warehouse, at: dt.datetime | None = None, limit: int = 600) -> list[dict]:
    """Where every tracked train is at instant `at`, interpolated between stops.

    Only observations published at or before `at` are considered, so scrubbing
    the slider backwards reproduces what was actually known then rather than
    back-dating later corrections.
    """
    at = at or dt.datetime.now(tz=dt.timezone.utc)

    rows = warehouse.fetchall(
        """
        WITH known AS (
            SELECT DISTINCT ON (trip_id, service_date, stop_sequence)
                   trip_id, service_date, stop_sequence,
                   COALESCE(departure_delay, arrival_delay) AS delay
            FROM   stop_time_updates
            WHERE  feed_timestamp <= %s
              AND  COALESCE(departure_delay, arrival_delay) IS NOT NULL
            ORDER  BY trip_id, service_date, stop_sequence, feed_timestamp DESC
        )
        SELECT k.trip_id, k.service_date, k.stop_sequence, k.delay,
               st.arrival_seconds, st.departure_seconds,
               s.stop_name, s.stop_lat, s.stop_lon,
               r.route_short_name, r.route_category
        FROM   known k
        JOIN   stop_times st ON st.trip_id = k.trip_id AND st.stop_sequence = k.stop_sequence
        JOIN   stops s       ON s.stop_id  = st.stop_id
        LEFT   JOIN trips t  ON t.trip_id  = k.trip_id
        LEFT   JOIN routes r ON r.route_id = t.route_id
        WHERE  s.stop_lat IS NOT NULL AND s.stop_lon IS NOT NULL
        ORDER  BY k.trip_id, k.service_date, k.stop_sequence
        """,
        (at,),
    )

    by_trip: dict[tuple[str, dt.date], list[tuple]] = {}
    for row in rows:
        by_trip.setdefault((row[0], row[1]), []).append(row)

    positions: list[dict] = []
    for (trip_id, service_date), calls in by_trip.items():
        placed = _place_train(trip_id, service_date, calls, at)
        if placed:
            positions.append(placed)
        if len(positions) >= limit:
            break
    return positions


def _place_train(trip_id: str, service_date: dt.date, calls: list[tuple], at: dt.datetime) -> dict | None:
    """Find the segment containing `at` and interpolate along it."""
    if len(calls) < 2:
        return None

    # Actual time at each call = scheduled time + the delay observed there.
    timeline = []
    for _, _, seq, delay, arr_s, dep_s, name, lat, lon, route_name, category in calls:
        if lat is None or lon is None:
            return None
        arrive = static_gtfs.absolute_time(service_date, arr_s) + dt.timedelta(seconds=delay) if arr_s is not None else None
        depart = static_gtfs.absolute_time(service_date, dep_s) + dt.timedelta(seconds=delay) if dep_s is not None else None
        timeline.append({
            "seq": seq, "name": name, "lat": lat, "lon": lon, "delay": delay,
            "arrive": arrive or depart, "depart": depart or arrive,
            "route_name": route_name, "category": category,
        })

    first, last = timeline[0], timeline[-1]
    meta = {
        "trip_id": trip_id,
        "route_name": last["route_name"] or trip_id,
        "route_category": last["category"] or "",
        "service_date": service_date.isoformat(),
    }

    # Before departure or after arrival: park the marker at the terminus rather
    # than dropping the train off the map entirely.
    if first["depart"] and at <= first["depart"]:
        return {**meta, "lat": first["lat"], "lon": first["lon"], "progress": 0.0,
                "from_stop": first["name"], "to_stop": timeline[1]["name"],
                "delay_seconds": first["delay"], "bearing": _bearing((first["lat"], first["lon"]), (timeline[1]["lat"], timeline[1]["lon"])),
                "status": "not_departed"}
    if last["arrive"] and at >= last["arrive"]:
        prev = timeline[-2]
        return {**meta, "lat": last["lat"], "lon": last["lon"], "progress": 1.0,
                "from_stop": prev["name"], "to_stop": last["name"],
                "delay_seconds": last["delay"], "bearing": _bearing((prev["lat"], prev["lon"]), (last["lat"], last["lon"])),
                "status": "arrived"}

    for current, nxt in zip(timeline, timeline[1:]):
        depart, arrive = current["depart"], nxt["arrive"]
        if not depart or not arrive or at < depart or at > arrive:
            continue
        span = (arrive - depart).total_seconds()
        progress = 0.0 if span <= 0 else min(1.0, max(0.0, (at - depart).total_seconds() / span))
        lat, lon = _interpolate((current["lat"], current["lon"]), (nxt["lat"], nxt["lon"]), progress)
        return {**meta, "lat": lat, "lon": lon, "progress": progress,
                "from_stop": current["name"], "to_stop": nxt["name"],
                "delay_seconds": nxt["delay"],
                "bearing": _bearing((current["lat"], current["lon"]), (nxt["lat"], nxt["lon"])),
                "status": "running"}

    # Dwelling at a station: `at` falls between an arrival and the next departure.
    for call in timeline:
        if call["arrive"] and call["depart"] and call["arrive"] <= at <= call["depart"]:
            return {**meta, "lat": call["lat"], "lon": call["lon"], "progress": 1.0,
                    "from_stop": call["name"], "to_stop": call["name"],
                    "delay_seconds": call["delay"], "bearing": 0.0, "status": "at_station"}
    return None


def network_geometry(warehouse) -> dict[str, Any]:
    """The rail network as GeoJSON LineStrings, one per physical link.

    Consecutive calls are paired with LEAD() and de-duplicated in SQL: hundreds
    of trips run over the same track, and the map only needs each link once.
    """
    rows = warehouse.fetchall(
        """
        WITH links AS (
            SELECT stop_id AS from_id,
                   LEAD(stop_id) OVER (PARTITION BY trip_id ORDER BY stop_sequence) AS to_id
            FROM   stop_times
        )
        SELECT DISTINCT a.stop_lon, a.stop_lat, b.stop_lon, b.stop_lat
        FROM   links l
        JOIN   stops a ON a.stop_id = l.from_id
        JOIN   stops b ON b.stop_id = l.to_id
        WHERE  l.to_id IS NOT NULL
          AND  a.stop_lat IS NOT NULL AND a.stop_lon IS NOT NULL
          AND  b.stop_lat IS NOT NULL AND b.stop_lon IS NOT NULL
        """
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "LineString", "coordinates": [[r[0], r[1]], [r[2], r[3]]]},
            }
            for r in rows
        ],
    }


def station_points(warehouse, min_calls: int = 1) -> dict[str, Any]:
    """Stations as GeoJSON Points, weighted by how much traffic they see."""
    rows = warehouse.fetchall(
        """
        SELECT s.stop_id, s.stop_name, s.stop_lon, s.stop_lat, count(st.trip_id) AS calls
        FROM   stops s
        JOIN   stop_times st ON st.stop_id = s.stop_id
        WHERE  s.stop_lat IS NOT NULL AND s.stop_lon IS NOT NULL
        GROUP  BY s.stop_id, s.stop_name, s.stop_lon, s.stop_lat
        HAVING count(st.trip_id) >= %s
        ORDER  BY calls DESC
        """,
        (min_calls,),
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"stop_id": r[0], "name": r[1], "calls": r[4]},
                "geometry": {"type": "Point", "coordinates": [r[2], r[3]]},
            }
            for r in rows
        ],
    }


def recent_polls(warehouse, limit: int = 60) -> list[dict]:
    """Recent ingestion attempts, oldest first, for the feed-health chart."""
    rows = warehouse.fetchall(
        """SELECT fetched_at, rows_written, duration_ms, payload_bytes, entity_count, error
           FROM   feed_polls
           ORDER  BY id DESC
           LIMIT  %s""",
        (limit,),
    )
    return [
        {
            "fetched_at": r[0].isoformat(),
            "rows_written": r[1] or 0,
            "duration_ms": r[2] or 0,
            "payload_bytes": r[3] or 0,
            "entity_count": r[4] or 0,
            "error": r[5],
        }
        for r in reversed(rows)   # newest-first from SQL, oldest-first for plotting
    ]
