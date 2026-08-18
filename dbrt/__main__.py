"""Collector entrypoint:  python -m dbrt [--once] [--iterations N]"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .collector import collect_once, run_forever
from .config import Settings
from .feed_client import FeedClient
from .static_gtfs import StaticTimetable, download_static
from .storage import Warehouse

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect DB Fernverkehr GTFS-RT data into PostgreSQL")
    parser.add_argument("--once", action="store_true", help="run a single poll and exit")
    parser.add_argument("--iterations", type=int, default=None, help="stop after N polls")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    log = logging.getLogger("dbrt")

    settings = Settings.from_env()
    source = settings.realtime_source()
    log.info("realtime source: %s", source.label)

    zip_path = download_static(settings, DATA_DIR / "gtfs_static.zip")
    # stop_times is loaded so the dashboard map can interpolate train positions.
    timetable = StaticTimetable.from_zip(zip_path, load_stop_times=True)
    log.info(
        "timetable: %d routes, %d trips (%d long-distance), %d stops, %d scheduled calls",
        len(timetable.routes), len(timetable.trips),
        len(timetable.long_distance_trip_ids()), len(timetable.stops),
        sum(len(c) for c in timetable.stop_times.values()),
    )

    warehouse = Warehouse(settings.dsn())
    warehouse.apply_schema()
    warehouse.load_timetable(timetable)

    client = FeedClient(settings)
    if args.once:
        summary = collect_once(client, timetable, warehouse)
        log.info("%s", summary)
    else:
        run_forever(settings, timetable, warehouse, client=client, iterations=args.iterations)


if __name__ == "__main__":
    main()
