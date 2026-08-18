"""HTTPS access to the GTFS-RT endpoint.

DB documents polling the realtime feed every 20s with an ETag. Conditional
requests matter more than usual here: the open fallback feed is ~45 MB, so a
304 turns a poll from tens of megabytes into a few hundred bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from .config import Settings

REQUEST_TIMEOUT_SECONDS = 60  # The full-Germany fallback payload is large.
USER_AGENT = "dbrt/0.1 (+https://github.com/spacedust10/deutsche-bahn-realtime-analytics)"


@dataclass(frozen=True)
class FeedResult:
    payload: Optional[bytes]
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
        self.etag: Optional[str] = None

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
