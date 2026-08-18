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
from typing import Any, Optional

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
    rows = warehouse.fetchall(
        f"""
        SELECT COALESCE(s.stop_name, d.stop_id) AS stop_name,
               d.stop_id, s.stop_lat, s.stop_lon,
               count(*) AS observations,
               avg({_DELAY})::float AS mean_delay,
               max({_DELAY}) AS max_delay,
               100.0 * count(*) FILTER (WHERE {_DELAY} < %s) / count(*) AS punctuality
        FROM   current_stop_delays d
        LEFT   JOIN stops s ON s.stop_id = d.stop_id
        WHERE  {_DELAY} IS NOT NULL
          AND  d.feed_timestamp > now() - make_interval(hours => %s)
        GROUP  BY 1, 2, 3, 4
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


def delay_propagation(warehouse, trip_id: str, service_date: Optional[dt.date] = None) -> list[dict]:
    """How one train's delay evolves stop by stop along its route."""
    rows = warehouse.fetchall(
        f"""
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
