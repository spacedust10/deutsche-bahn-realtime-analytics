"""The poll loop: fetch, decode, scope to long distance, persist, audit.

A collector that dies on a bad upstream response loses data for as long as
nobody is watching, so every failure mode here is caught, recorded in
feed_polls, and returned in the summary — the loop keeps going.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass

from .gtfs_rt import decode_feed, feed_timestamp, iter_stop_time_updates

log = logging.getLogger(__name__)


@dataclass
class PollSummary:
    feed_timestamp: dt.datetime | None = None
    entity_count: int = 0
    long_distance_trips: int = 0
    rows_written: int = 0
    payload_bytes: int = 0
    http_status: int | None = None
    not_modified: bool = False
    duration_ms: int = 0
    source_label: str = ""
    error: str | None = None


def collect_once(client, timetable, warehouse) -> PollSummary:
    """Run exactly one poll cycle and return what happened."""
    started = time.perf_counter()
    summary = PollSummary()

    try:
        result = client.fetch()
        summary.http_status = result.status
        summary.payload_bytes = result.bytes_downloaded
        summary.source_label = result.source_label

        if result.not_modified:
            # Feed unchanged since the last ETag; nothing new to store.
            summary.not_modified = True
        else:
            feed = decode_feed(result.payload)
            summary.feed_timestamp = feed_timestamp(feed)
            summary.entity_count = len(feed.entity)

            batch = []
            trips = set()
            for record in iter_stop_time_updates(feed):
                category = timetable.category_for_trip(record.trip_id)
                # Scope enforcement: the open fallback feed carries every German
                # operator, so anything without a long-distance category is dropped.
                if not timetable.is_long_distance(record.trip_id):
                    continue
                trips.add(record.trip_id)
                batch.append((record, summary.feed_timestamp, category))

            summary.long_distance_trips = len(trips)
            summary.rows_written = warehouse.insert_stop_time_updates(batch)

    except Exception as exc:  # noqa: BLE001 - the loop must survive any upstream fault
        summary.error = str(exc)
        log.warning("poll failed: %s", exc)

    summary.duration_ms = int((time.perf_counter() - started) * 1000)

    warehouse.record_poll(
        feed_timestamp=summary.feed_timestamp,
        source_url=summary.source_label,
        http_status=summary.http_status,
        payload_bytes=summary.payload_bytes,
        entity_count=summary.entity_count,
        long_distance_trips=summary.long_distance_trips,
        rows_written=summary.rows_written,
        duration_ms=summary.duration_ms,
        error=summary.error,
    )
    return summary


def run_forever(settings, timetable, warehouse, client=None, iterations: int | None = None) -> None:
    """Poll on a fixed interval until interrupted (or `iterations` polls elapse).

    Sleeps for the remainder of the interval rather than a flat interval, so a
    slow poll does not compound into drift.
    """
    from .feed_client import FeedClient

    client = client or FeedClient(settings)
    completed = 0

    while iterations is None or completed < iterations:
        summary = collect_once(client, timetable, warehouse)
        if summary.error:
            log.warning("poll error: %s", summary.error)
        elif summary.not_modified:
            log.info("feed unchanged (304)")
        else:
            log.info(
                "feed %s | %d long-distance trips | %d rows | %d ms",
                summary.feed_timestamp, summary.long_distance_trips,
                summary.rows_written, summary.duration_ms,
            )

        completed += 1
        if iterations is not None and completed >= iterations:
            break
        time.sleep(max(0.0, settings.poll_interval_seconds - summary.duration_ms / 1000))
