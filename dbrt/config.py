"""Runtime settings, read from the environment.

The architecture calls for the official DB Fernverkehr GTFS-RT feed, which is
key-gated. To keep the pipeline runnable without credentials we fall back to
the open long-distance feed; `Source.official` records which one is in use so
every downstream consumer (and the dashboard) can report data lineage honestly.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

OFFICIAL_RT_URL = "https://gtfs-datenstroeme.tech.deutschebahn.com/db-fernverkehr/gtfsrt_trip_updates.proto"
OFFICIAL_ALERTS_URL = "https://gtfs-datenstroeme.tech.deutschebahn.com/db-fernverkehr/gtfsrt_service_alerts.proto"
OFFICIAL_STATIC_URL = "https://gtfs-datenstroeme.tech.deutschebahn.com/db-fernverkehr/gtfs.zip"
FALLBACK_RT_URL = "https://realtime.gtfs.de/realtime-free.pb"
FALLBACK_STATIC_URL = "https://download.gtfs.de/germany/fv_free/latest.zip"

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_POLL_SECONDS = 30
MIN_POLL_SECONDS = 10  # Politeness floor; DB documents 20s for the official feed.
# The open fallback ships all of Germany uncompressed (~44 MB, ~9s to pull), so a
# true 10s upstream poll would saturate the link continuously against a
# donation-funded server. The dashboard refreshes at 10s regardless; only the
# fetch is slower, and the UI reports how old the data actually is.


@dataclass(frozen=True)
class Source:
    """A resolved feed endpoint plus the headers needed to read it."""

    url: str
    headers: dict[str, str]
    official: bool

    @property
    def label(self) -> str:
        return "DB Fernverkehr (official)" if self.official else "gtfs.de long-distance (open)"


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    db_gtfs_rt_url: str = OFFICIAL_RT_URL
    db_gtfs_alerts_url: str = OFFICIAL_ALERTS_URL
    db_gtfs_static_url: str = OFFICIAL_STATIC_URL
    fallback_gtfs_rt_url: str = FALLBACK_RT_URL
    fallback_gtfs_static_url: str = FALLBACK_STATIC_URL
    poll_interval_seconds: int = DEFAULT_POLL_SECONDS
    pghost: str = "localhost"
    pgport: str = "5432"
    pgdatabase: str = "dbrt"
    pguser: str = ""
    pgpassword: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: Path | None = None,
    ) -> Settings:
        if env is None:
            # Real environment wins: an explicit export must not be overridden
            # by a stale .env someone forgot about.
            env = {**load_dotenv(dotenv_path or DEFAULT_ENV_FILE), **os.environ}

        def get(key: str, default: str) -> str:
            value = env.get(key)
            return default if value is None or value == "" else value

        return cls(
            api_key=env.get("DB_API_KEY", "").strip(),
            db_gtfs_rt_url=get("DB_GTFS_RT_URL", OFFICIAL_RT_URL),
            db_gtfs_alerts_url=get("DB_GTFS_ALERTS_URL", OFFICIAL_ALERTS_URL),
            db_gtfs_static_url=get("DB_GTFS_STATIC_URL", OFFICIAL_STATIC_URL),
            fallback_gtfs_rt_url=get("FALLBACK_GTFS_RT_URL", FALLBACK_RT_URL),
            fallback_gtfs_static_url=get("FALLBACK_GTFS_STATIC_URL", FALLBACK_STATIC_URL),
            poll_interval_seconds=_positive_int(env.get("POLL_INTERVAL_SECONDS"), DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS),
            pghost=get("PGHOST", "localhost"),
            pgport=get("PGPORT", "5432"),
            pgdatabase=get("PGDATABASE", "dbrt"),
            pguser=env.get("PGUSER", "") or "",
            pgpassword=env.get("PGPASSWORD", "") or "",
            api_host=get("API_HOST", "127.0.0.1"),
            api_port=_positive_int(env.get("API_PORT"), 8000, 1),
        )

    def realtime_source(self) -> Source:
        if self.api_key:
            return Source(self.db_gtfs_rt_url, {"DB-Api-Key": self.api_key}, official=True)
        return Source(self.fallback_gtfs_rt_url, {}, official=False)

    def static_source(self) -> Source:
        if self.api_key:
            return Source(self.db_gtfs_static_url, {"DB-Api-Key": self.api_key}, official=True)
        return Source(self.fallback_gtfs_static_url, {}, official=False)

    def dsn(self) -> str:
        parts = [f"host={self.pghost}", f"port={self.pgport}", f"dbname={self.pgdatabase}"]
        if self.pguser:
            parts.append(f"user={self.pguser}")
        if self.pgpassword:
            parts.append(f"password={self.pgpassword}")
        return " ".join(parts)


def _positive_int(raw: str | None, default: int, minimum: int) -> int:
    """Bad config should degrade to the default, not take the collector down."""
    try:
        return max(int(str(raw).strip()), minimum)
    except (TypeError, ValueError):
        return default


def load_dotenv(path: Path | str) -> dict[str, str]:
    """Minimal KEY=VALUE reader.

    Deliberately not python-dotenv: this is eight lines of parsing against a
    file format we also author, and one fewer dependency in the deployment.
    """
    path = Path(path)
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")  # partition keeps '=' inside values
        values[key.strip()] = value.strip().strip("\"'")
    return values
