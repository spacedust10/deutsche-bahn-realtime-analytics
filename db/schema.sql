-- ---------------------------------------------------------------------------
-- Deutsche Bahn Fernverkehr realtime warehouse
--
-- Static GTFS tables (routes/stops/trips) are reference data, refreshed daily
-- from the timetable ZIP. stop_time_updates is the append-only fact table fed
-- by the GTFS-RT stream; every row is one observation of one stop of one trip
-- at one feed timestamp, which is what makes delay *evolution* analysable
-- rather than just the latest state.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS routes (
    route_id         TEXT PRIMARY KEY,
    route_short_name TEXT,
    route_long_name  TEXT,
    route_category   TEXT,          -- ICE / IC / EC / ECE / EN / RJ
    agency_id        TEXT,
    route_type       INTEGER
);

CREATE TABLE IF NOT EXISTS stops (
    stop_id        TEXT PRIMARY KEY,
    stop_name      TEXT NOT NULL,
    stop_lat       DOUBLE PRECISION,
    stop_lon       DOUBLE PRECISION,
    parent_station TEXT
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id    TEXT PRIMARY KEY,
    route_id   TEXT REFERENCES routes(route_id),
    service_id TEXT
);

-- One row per (trip, service date, stop, feed timestamp).
-- The feed timestamp in the key makes re-ingesting an unchanged feed a no-op,
-- so the collector is safely restartable and idempotent.
CREATE TABLE IF NOT EXISTS stop_time_updates (
    trip_id             TEXT        NOT NULL,
    service_date        DATE        NOT NULL,
    stop_sequence       INTEGER     NOT NULL,
    feed_timestamp      TIMESTAMPTZ NOT NULL,
    stop_id             TEXT,
    arrival_delay       INTEGER,    -- seconds, negative = early
    departure_delay     INTEGER,
    arrival_time        TIMESTAMPTZ,
    departure_time      TIMESTAMPTZ,
    schedule_relationship TEXT,     -- SCHEDULED / SKIPPED / NO_DATA
    trip_schedule_relationship TEXT,
    route_category      TEXT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trip_id, service_date, stop_sequence, feed_timestamp)
);

-- Ingestion audit trail; also powers the dashboard's feed-health panel.
CREATE TABLE IF NOT EXISTS feed_polls (
    id                 BIGSERIAL PRIMARY KEY,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    feed_timestamp     TIMESTAMPTZ,
    source_url         TEXT,
    http_status        INTEGER,
    payload_bytes      BIGINT,
    entity_count       INTEGER,
    long_distance_trips INTEGER,
    rows_written       INTEGER,
    duration_ms        INTEGER,
    error              TEXT
);

CREATE INDEX IF NOT EXISTS idx_stu_feed_ts     ON stop_time_updates (feed_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_stu_stop        ON stop_time_updates (stop_id);
CREATE INDEX IF NOT EXISTS idx_stu_trip_date   ON stop_time_updates (trip_id, service_date);
CREATE INDEX IF NOT EXISTS idx_stu_category    ON stop_time_updates (route_category);
CREATE INDEX IF NOT EXISTS idx_polls_fetched   ON feed_polls (fetched_at DESC);

-- Matches the ORDER BY of current_stop_delays exactly, so the DISTINCT ON below
-- resolves by index scan instead of sorting the whole fact table. Without it
-- every dashboard query degrades as history accumulates: measured at 417k rows,
-- the full payload went from 6.3s to 2.0s.
CREATE INDEX IF NOT EXISTS idx_stu_latest
    ON stop_time_updates (trip_id, service_date, stop_sequence, feed_timestamp DESC);

-- Latest observation per (trip, service date, stop): the "now" state of the
-- network, without collapsing the history that feeds ML and propagation work.
CREATE OR REPLACE VIEW current_stop_delays AS
SELECT DISTINCT ON (trip_id, service_date, stop_sequence)
       trip_id, service_date, stop_sequence, stop_id,
       arrival_delay, departure_delay, schedule_relationship,
       route_category, feed_timestamp
FROM   stop_time_updates
ORDER  BY trip_id, service_date, stop_sequence, feed_timestamp DESC;

-- ---------------------------------------------------------------------------
-- Scheduled calls, loaded from the static timetable.
--
-- Needed only by the live map: interpolating a train's position between two
-- stations requires knowing when it was *supposed* to be at each of them, which
-- the realtime feed never carries (GTFS-RT publishes delays, not coordinates).
-- Times are seconds from the start of the service day and may exceed 86400.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stop_times (
    trip_id           TEXT    NOT NULL,
    stop_sequence     INTEGER NOT NULL,
    stop_id           TEXT    NOT NULL,
    arrival_seconds   INTEGER,
    departure_seconds INTEGER,
    PRIMARY KEY (trip_id, stop_sequence)
);

CREATE INDEX IF NOT EXISTS idx_stop_times_trip ON stop_times (trip_id);
