"""PostgreSQL warehouse.

Writes go through `execute_values` batching because a single poll of the
long-distance feed produces a few thousand stop-time rows, and round-tripping
those one INSERT at a time is the difference between 40 ms and 40 s.

Idempotency is enforced by the primary key rather than by application logic:
`ON CONFLICT DO NOTHING` on (trip_id, service_date, stop_sequence,
feed_timestamp) means replaying a feed is free and the collector can crash and
restart without corrupting history.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterable, Optional, Sequence

import psycopg2
from psycopg2.extras import execute_values

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

_TABLES = ("stop_time_updates", "feed_polls", "trips", "routes", "stops")
_PAGE_SIZE = 1000

_INSERT_STU = """
INSERT INTO stop_time_updates (
    trip_id, service_date, stop_sequence, feed_timestamp, stop_id,
    arrival_delay, departure_delay, arrival_time, departure_time,
    schedule_relationship, trip_schedule_relationship, route_category
) VALUES %s
ON CONFLICT (trip_id, service_date, stop_sequence, feed_timestamp) DO NOTHING
"""


class Warehouse:
    def __init__(self, dsn: str):
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    # --- lifecycle ---------------------------------------------------------

    def apply_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text())

    def truncate_all(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")

    def close(self) -> None:
        self.conn.close()

    # --- writes ------------------------------------------------------------

    def insert_stop_time_updates(self, batch: Sequence[tuple]) -> int:
        """`batch` is (record, feed_timestamp, route_category) triples.

        Returns rows actually written, which is < len(batch) whenever the feed
        republished an unchanged timestamp.
        """
        if not batch:
            return 0
        rows = [
            (
                rec.trip_id, rec.service_date, rec.stop_sequence, feed_ts, rec.stop_id,
                rec.arrival_delay, rec.departure_delay, rec.arrival_time, rec.departure_time,
                rec.schedule_relationship, rec.trip_schedule_relationship, category,
            )
            for rec, feed_ts, category in batch
        ]
        written = 0
        with self.conn.cursor() as cur:
            # execute_values pages internally and leaves cur.rowcount reflecting
            # only the final page, so pages are driven here and summed instead.
            for start in range(0, len(rows), _PAGE_SIZE):
                execute_values(cur, _INSERT_STU, rows[start:start + _PAGE_SIZE], page_size=_PAGE_SIZE)
                written += cur.rowcount
        return written

    def load_timetable(self, timetable) -> None:
        """Refresh GTFS reference data. Upserts so a daily reload is safe."""
        with self.conn.cursor() as cur:
            execute_values(
                cur,
                """INSERT INTO routes (route_id, route_short_name, route_long_name, route_category, agency_id, route_type)
                   VALUES %s ON CONFLICT (route_id) DO UPDATE SET
                     route_short_name = EXCLUDED.route_short_name,
                     route_long_name  = EXCLUDED.route_long_name,
                     route_category   = EXCLUDED.route_category,
                     agency_id        = EXCLUDED.agency_id,
                     route_type       = EXCLUDED.route_type""",
                [(r.route_id, r.route_short_name, r.route_long_name, r.category, r.agency_id, r.route_type)
                 for r in timetable.routes.values()],
                page_size=1000,
            )
            execute_values(
                cur,
                """INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon, parent_station)
                   VALUES %s ON CONFLICT (stop_id) DO UPDATE SET
                     stop_name = EXCLUDED.stop_name,
                     stop_lat  = EXCLUDED.stop_lat,
                     stop_lon  = EXCLUDED.stop_lon,
                     parent_station = EXCLUDED.parent_station""",
                [(s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, s.parent_station)
                 for s in timetable.stops.values()],
                page_size=1000,
            )
            # Trips reference routes, so they load last.
            execute_values(
                cur,
                """INSERT INTO trips (trip_id, route_id, service_id)
                   VALUES %s ON CONFLICT (trip_id) DO UPDATE SET
                     route_id = EXCLUDED.route_id, service_id = EXCLUDED.service_id""",
                [(t.trip_id, t.route_id or None, t.service_id) for t in timetable.trips.values()],
                page_size=1000,
            )

    def record_poll(
        self,
        feed_timestamp: Optional[dt.datetime] = None,
        source_url: str = "",
        http_status: Optional[int] = None,
        payload_bytes: int = 0,
        entity_count: int = 0,
        long_distance_trips: int = 0,
        rows_written: int = 0,
        duration_ms: int = 0,
        error: Optional[str] = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO feed_polls (feed_timestamp, source_url, http_status, payload_bytes,
                       entity_count, long_distance_trips, rows_written, duration_ms, error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (feed_timestamp, source_url, http_status, payload_bytes,
                 entity_count, long_distance_trips, rows_written, duration_ms, error),
            )

    # --- reads -------------------------------------------------------------

    def fetchall(self, sql: str, params: Iterable = ()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchall()

    def fetchone(self, sql: str, params: Iterable = ()) -> Optional[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchone()

    def count(self, table: str) -> int:
        if table not in _TABLES:
            raise ValueError(f"unknown table: {table}")  # Never interpolate caller input.
        return self.fetchone(f"SELECT count(*) FROM {table}")[0]

    def latest_feed_timestamp(self) -> Optional[dt.datetime]:
        row = self.fetchone("SELECT max(feed_timestamp) FROM stop_time_updates")
        return row[0] if row else None
