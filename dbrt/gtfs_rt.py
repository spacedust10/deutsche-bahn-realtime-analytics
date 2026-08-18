"""GTFS-Realtime Protocol Buffer decoding.

Wire format is defined by the GTFS-Realtime spec (gtfs.org/realtime/reference).
We use the official `gtfs-realtime-bindings` package rather than hand-rolling
the .proto so the schema tracks upstream.

The decoder deliberately distinguishes "no prediction" (None) from "on time"
(0). Conflating them would silently bias every punctuality number downstream.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as gtfs_rt

# Enum name lookups, kept local so callers never touch protobuf internals.
_STOP_REL = gtfs_rt.TripUpdate.StopTimeUpdate.ScheduleRelationship
_TRIP_REL = gtfs_rt.TripDescriptor.ScheduleRelationship


@dataclass(frozen=True)
class StopTimeUpdateRecord:
    """One observation of one stop of one trip, as published by the feed."""

    trip_id: str
    service_date: dt.date
    stop_sequence: int
    stop_id: str | None
    arrival_delay: int | None
    departure_delay: int | None
    arrival_time: dt.datetime | None
    departure_time: dt.datetime | None
    schedule_relationship: str
    trip_schedule_relationship: str
    route_id: str | None


def decode_feed(payload: bytes) -> gtfs_rt.FeedMessage:
    """Parse raw feed bytes into a FeedMessage.

    Upstream outages tend to arrive as an HTML error page with a 200 status, so
    a parse failure is reported as a clear ValueError instead of a protobuf
    DecodeError leaking through the collector.
    """
    if not payload:
        raise ValueError("empty payload: not a valid GTFS-RT feed")
    feed = gtfs_rt.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except (DecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"payload is not a valid GTFS-RT feed: {exc}") from exc
    if not feed.header.gtfs_realtime_version:
        raise ValueError("payload is not a valid GTFS-RT feed: missing header version")
    return feed


def feed_timestamp(feed: gtfs_rt.FeedMessage) -> dt.datetime:
    """Feed publication time as a timezone-aware UTC datetime."""
    return dt.datetime.fromtimestamp(feed.header.timestamp, tz=dt.timezone.utc)


def iter_stop_time_updates(feed: gtfs_rt.FeedMessage) -> Iterator[StopTimeUpdateRecord]:
    """Flatten trip updates into one record per stop.

    Entities carrying alerts or vehicle positions are skipped: only TripUpdates
    contribute to the delay fact table.
    """
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        trip = trip_update.trip
        service_date = _parse_service_date(trip.start_date)
        trip_rel = _TRIP_REL.Name(trip.schedule_relationship)

        for stu in trip_update.stop_time_update:
            yield StopTimeUpdateRecord(
                trip_id=trip.trip_id,
                service_date=service_date,
                stop_sequence=stu.stop_sequence,
                stop_id=stu.stop_id or None,
                arrival_delay=_delay(stu, "arrival"),
                departure_delay=_delay(stu, "departure"),
                arrival_time=_event_time(stu, "arrival"),
                departure_time=_event_time(stu, "departure"),
                schedule_relationship=_STOP_REL.Name(stu.schedule_relationship),
                trip_schedule_relationship=trip_rel,
                route_id=trip.route_id or None,
            )


def _delay(stu, field: str) -> int | None:
    """Delay in seconds, or None when the feed makes no prediction for this event."""
    if not stu.HasField(field):
        return None
    event = getattr(stu, field)
    return event.delay if event.HasField("delay") else None


def _event_time(stu, field: str) -> dt.datetime | None:
    if not stu.HasField(field):
        return None
    event = getattr(stu, field)
    if not event.HasField("time") or event.time == 0:
        return None
    return dt.datetime.fromtimestamp(event.time, tz=dt.timezone.utc)


def _parse_service_date(raw: str) -> dt.date:
    """GTFS start_date is YYYYMMDD; fall back to today when the feed omits it."""
    try:
        return dt.datetime.strptime(raw, "%Y%m%d").date()
    except (ValueError, TypeError):
        return dt.datetime.now(tz=dt.timezone.utc).date()
