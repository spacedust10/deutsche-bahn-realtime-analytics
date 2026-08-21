"""HTTPS access to the DB data streams: the realtime feed and the timetable.

This is the only module that talks HTTP. Keeping the timetable download here
rather than in static_gtfs leaves that module a pure parser, testable against a
local zip without a network or an HTTP library.

DB documents polling the realtime feed every 20s with an ETag. Conditional
requests matter more than usual here: the open fallback feed is ~45 MB, so a
304 turns a poll from tens of megabytes into a few hundred bytes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import Settings

REQUEST_TIMEOUT_SECONDS = 60  # The full-Germany fallback payload is large.
USER_AGENT = "dbrt/0.1 (+https://github.com/spacedust10/deutsche-bahn-realtime-analytics)"


@dataclass(frozen=True)
class FeedResult:
    payload: bytes | None
    status: int
    not_modified: bool
    source_label: str
    official: bool
    bytes_downloaded: int


class FeedClient:
    """Polls one GTFS-RT endpoint, remembering the last ETag it saw."""

    def __init__(self, settings: Settings, session=None):
        self.settings = settings
        self.session = session or requests.Session()
        self.etag: str | None = None

    def fetch(self) -> FeedResult:
        source = self.settings.realtime_source()
        headers = {"User-Agent": USER_AGENT, **source.headers}
        if self.etag:
            headers["If-None-Match"] = self.etag

        response = self.session.get(source.url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)

        if response.status_code == 304:
            return FeedResult(None, 304, True, source.label, source.official, 0)

        response.raise_for_status()
        # Only adopt a new ETag from a real body; a 304 must leave it intact.
        self.etag = response.headers.get("ETag", self.etag)
        payload = response.content
        return FeedResult(payload, response.status_code, False, source.label, source.official, len(payload))


# --- static timetable ------------------------------------------------------

STATIC_MAX_AGE_SECONDS = 12 * 3600  # DB regenerates the timetable daily.
DOWNLOAD_TIMEOUT_SECONDS = 120


def download_static(
    settings: Settings,
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
