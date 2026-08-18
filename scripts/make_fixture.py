"""Carve a small, committable GTFS-RT fixture out of a live feed capture.

Tests should run offline against real upstream bytes rather than hand-rolled
protobufs, but the full German feed is ~45 MB. This keeps a handful of genuine
long-distance trips (plus one alert) so the decoder is exercised on real data.

Usage:
    python scripts/make_fixture.py <live_feed.pb> <static_gtfs_dir> <out.pb>
"""
import csv
import sys
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as pb

MAX_TRIPS = 25
MAX_ALERTS = 3


def main(feed_path: str, static_dir: str, out_path: str) -> None:
    source = pb.FeedMessage()
    source.ParseFromString(Path(feed_path).read_bytes())

    long_distance = {row["trip_id"] for row in csv.DictReader(open(Path(static_dir) / "trips.txt"))}

    out = pb.FeedMessage()
    out.header.CopyFrom(source.header)
    trips = alerts = 0
    for entity in source.entity:
        if entity.HasField("trip_update") and trips < MAX_TRIPS:
            if entity.trip_update.trip.trip_id in long_distance:
                out.entity.add().CopyFrom(entity)
                trips += 1
        elif entity.HasField("alert") and alerts < MAX_ALERTS:
            out.entity.add().CopyFrom(entity)
            alerts += 1
        if trips >= MAX_TRIPS and alerts >= MAX_ALERTS:
            break

    Path(out_path).write_bytes(out.SerializeToString())
    print(f"wrote {out_path}: {trips} trip updates, {alerts} alerts, {Path(out_path).stat().st_size} bytes")


if __name__ == "__main__":
    main(*sys.argv[1:4])
