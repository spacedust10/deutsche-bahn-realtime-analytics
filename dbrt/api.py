"""Dashboard backend: REST analytics plus a WebSocket that pushes live state.

The WebSocket exists because the dashboard is a *realtime* view: polling six
REST endpoints on a timer from the browser would triple the query load and
still lag the collector. One push carries the whole dashboard payload.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import analytics, domain
from .config import Settings
from .ml import DelayModel
from .storage import Warehouse

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "delay_model.joblib"

# The upstream GTFS-RT stream republishes every 10s, so pushing faster than
# that shows the same numbers twice. This is the dashboard's heartbeat.
PUSH_INTERVAL_SECONDS = 10

# The map draws every tracked train. Overnight services from a previous service
# date stay live, so this sits above the ~500 running at a weekday peak; the
# dashboard reports the real count separately so a cap can never masquerade as a
# measurement.
POSITION_LIMIT = 1200

# Bounded and long-lived on purpose. Warehouse hands each thread its own
# connection, so a fresh pool per request would open a new set every time and
# never give them back: that exhausted PostgreSQL's 100-client limit within a
# few refreshes. Reusing the threads reuses their connections.
PAYLOAD_WORKERS = 6


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    # ponytail: one shared autocommit connection. Fine for a single-process
    # dashboard; swap for a psycopg2 ThreadedConnectionPool if this ever fans
    # out to multiple workers.
    state: dict[str, Any] = {"warehouse": None, "model": None, "pool": None}

    def pool() -> ThreadPoolExecutor:
        if state["pool"] is None:
            state["pool"] = ThreadPoolExecutor(
                max_workers=PAYLOAD_WORKERS, thread_name_prefix="payload"
            )
        return state["pool"]

    def warehouse() -> Warehouse:
        if state["warehouse"] is None:
            state["warehouse"] = Warehouse(settings.dsn())
        return state["warehouse"]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        warehouse()
        if MODEL_PATH.exists():
            try:
                state["model"] = DelayModel.load(MODEL_PATH)
                log.info("loaded delay model from %s", MODEL_PATH)
            except Exception as exc:  # noqa: BLE001 - a stale model must not block the API
                log.warning("could not load delay model: %s", exc)
        yield
        if state["pool"] is not None:
            state["pool"].shutdown(wait=False)
        if state["warehouse"] is not None:
            state["warehouse"].close()

    app = FastAPI(title="DB Fernverkehr Realtime Analytics", version="0.1.0", lifespan=lifespan)

    # The page, its script and its stylesheet change together, and they only
    # work together: the markup names the panels, the script fills them. Served
    # with nothing but an ETag, browsers apply heuristic freshness and reuse a
    # cached script for hours without asking. That shipped new panels against an
    # old app.js, which rendered every heading and filled none of them, leaving
    # four charts blank under "Waiting for data…" while the API was healthy.
    #
    # `no-cache` does not disable caching; it requires revalidation, so the
    # usual answer is still a 304 with no body.
    @app.middleware("http")
    async def revalidate_dashboard(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # --- REST --------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        try:
            warehouse().fetchone("SELECT 1")
            connected = True
        except Exception:  # noqa: BLE001
            connected = False
        return {"status": "ok" if connected else "degraded", "database": connected}

    @app.get("/api/summary")
    def summary(hours: int = Query(24, ge=1, le=168)) -> dict:
        return _summary(warehouse(), settings, hours)

    @app.get("/api/timeseries")
    def timeseries(
        bucket: int = Query(5, ge=1, le=180),
        hours: int = Query(6, ge=1, le=168),
    ) -> list[dict]:
        return analytics.delay_timeseries(warehouse(), bucket_minutes=bucket, hours=hours)

    @app.get("/api/stations")
    def stations(
        limit: int = Query(15, ge=1, le=100),
        min_observations: int = Query(3, ge=1, le=1000),
        hours: int = Query(24, ge=1, le=168),
    ) -> list[dict]:
        return analytics.station_delays(warehouse(), limit=limit, min_observations=min_observations, hours=hours)

    @app.get("/api/stations/minutes")
    def station_minutes(
        limit: int = Query(12, ge=1, le=100),
        hours: int = Query(24, ge=1, le=168),
    ) -> list[dict]:
        """Stations by the delay minutes they carry, with a cumulative share."""
        return analytics.station_delay_minutes(warehouse(), limit=limit, hours=hours)

    @app.get("/api/origination")
    def origination(
        limit: int = Query(12, ge=1, le=100),
        min_steps: int = Query(3, ge=1, le=1000),
        hours: int = Query(24, ge=1, le=168),
    ) -> list[dict]:
        """Delay created at a station versus delay it inherited from upstream."""
        return analytics.delay_origination(warehouse(), limit=limit, min_steps=min_steps, hours=hours)

    @app.get("/api/heatmap")
    def heatmap(days: int = Query(7, ge=1, le=90)) -> list[dict]:
        """Punctuality by local hour and weekday.

        Not part of the WebSocket payload: it scans days of history to answer a
        question about weekly rhythm, and pushing it on the feed's 10s heartbeat
        would spend a second of database time to redraw an unchanged chart.
        """
        return analytics.punctuality_heatmap(warehouse(), days=days)

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        """The same payload the WebSocket pushes, for first paint and for
        clients where WebSockets are unavailable."""
        return _dashboard_payload(warehouse(), settings, pool())

    @app.get("/api/cancellations")
    def cancellations(hours: int = Query(24, ge=1, le=168)) -> dict:
        return {
            **analytics.cancellations(warehouse(), hours=hours),
            "most_skipped": analytics.skipped_stations(warehouse(), limit=10, hours=hours),
        }

    @app.get("/api/stations/geo")
    def station_geometry() -> list[dict]:
        return analytics.station_geometry(warehouse())

    @app.get("/api/categories")
    def categories(hours: int = Query(24, ge=1, le=168)) -> list[dict]:
        return analytics.category_breakdown(warehouse(), hours=hours)

    @app.get("/api/distribution")
    def distribution(hours: int = Query(24, ge=1, le=168)) -> list[dict]:
        return analytics.delay_distribution(warehouse(), hours=hours)

    @app.get("/api/network")
    def network(limit: int = Query(400, ge=1, le=2000)) -> list[dict]:
        return analytics.network_snapshot(warehouse(), limit=limit)

    @app.get("/api/trips/worst")
    def worst_trips(limit: int = Query(10, ge=1, le=100), hours: int = Query(24, ge=1, le=168)) -> list[dict]:
        return analytics.worst_trips(warehouse(), limit=limit, hours=hours)

    @app.get("/api/trips/{trip_id}/propagation")
    def propagation(trip_id: str, service_date: str | None = Query(None, description="YYYY-MM-DD; defaults to the newest run")) -> list[dict]:
        parsed = None
        if service_date:
            try:
                parsed = dt.date.fromisoformat(service_date)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"not a date: {service_date}") from None
        rows = analytics.delay_propagation(warehouse(), trip_id=trip_id, service_date=parsed)
        if not rows:
            raise HTTPException(status_code=404, detail=f"no observations for trip {trip_id}")
        return rows

    @app.get("/api/model")
    def model_info() -> dict:
        model = state["model"]
        if model is None:
            return {"trained": False, "detail": "no model on disk; run `make train`"}
        return {"trained": True, **model.metrics}

    @app.get("/api/rules")
    def rules() -> dict:
        """The domain's rules, published so clients derive rather than restate.

        The dashboard used to hold its own copies of these boundaries and they
        drifted: the map legend called anything under 3 minutes on time while
        the histogram and the headline metric used DB's 6.
        """
        return _rules()

    @app.get("/api/geo/network")
    def geo_network() -> dict:
        """Rail network as GeoJSON. Static between timetable reloads, so the
        dashboard fetches it once per page load rather than per push."""
        return analytics.network_geometry(warehouse())

    @app.get("/api/geo/stations")
    def geo_stations(min_calls: int = Query(1, ge=1, le=500)) -> dict:
        return analytics.station_points(warehouse(), min_calls=min_calls)

    @app.get("/api/geo/segments")
    def geo_segments(
        min_traversals: int = Query(3, ge=1, le=500),
        hours: int = Query(24, ge=1, le=168),
    ) -> dict:
        """Observed links as GeoJSON, carrying the time trains lose on each."""
        return analytics.segment_performance(warehouse(), min_traversals=min_traversals, hours=hours)

    @app.get("/api/positions")
    def positions(
        at: str | None = Query(None, description="ISO-8601 instant; defaults to now"),
        limit: int = Query(600, ge=1, le=2000),
    ) -> dict:
        """Interpolated train positions, either live or at a historical instant."""
        when = _parse_instant(at)
        return {
            "at": when.isoformat(),
            "positions": analytics.live_positions(warehouse(), at=when, limit=limit),
        }

    @app.get("/api/history/window")
    def history_window() -> dict:
        """Span the collected history covers — the range of the time slider."""
        return analytics.history_window(warehouse())

    # --- WebSocket ---------------------------------------------------------

    @app.websocket("/ws")
    async def live(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                payload = await asyncio.to_thread(_dashboard_payload, warehouse(), settings, pool())
                await ws.send_json(payload)
                await asyncio.sleep(PUSH_INTERVAL_SECONDS)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001 - never let one socket kill the server
            log.warning("websocket closed: %s", exc)

    # --- static dashboard --------------------------------------------------

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


def _rules() -> dict:
    """Serialise the domain layer for transport. No rule is decided here."""
    return {
        "punctuality_threshold_seconds": domain.PUNCTUALITY_THRESHOLD_SECONDS,
        "long_distance_categories": sorted(domain.LONG_DISTANCE_CATEGORIES),
        "delay_bands": [
            {
                "key": band.key,
                "label": band.label,
                "severity": band.severity,
                "lower_seconds": band.lower,
                "upper_seconds": band.upper,
            }
            for band in domain.DELAY_BANDS
        ],
    }


def _parse_instant(raw: str | None) -> dt.datetime:
    """Parse an ISO-8601 query param, defaulting to now and always tz-aware."""
    if not raw:
        return dt.datetime.now(tz=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"not an ISO-8601 instant: {raw}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _summary(warehouse: Warehouse, settings: Settings, hours: int = 24) -> dict:
    source = settings.realtime_source()
    poll = warehouse.fetchone(
        """SELECT fetched_at, feed_timestamp, http_status, entity_count,
                  long_distance_trips, rows_written, duration_ms, error
           FROM feed_polls ORDER BY id DESC LIMIT 1"""
    )
    feed = {
        "last_poll": poll[0].isoformat() if poll and poll[0] else None,
        "feed_timestamp": poll[1].isoformat() if poll and poll[1] else None,
        "http_status": poll[2] if poll else None,
        "entity_count": poll[3] if poll else 0,
        "long_distance_trips": poll[4] if poll else 0,
        "rows_written": poll[5] if poll else 0,
        "duration_ms": poll[6] if poll else 0,
        "error": poll[7] if poll else None,
    }
    return {
        "punctuality": analytics.punctuality(warehouse, hours=hours),
        "feed": feed,
        "source": {"label": source.label, "official": source.official, "url": source.url},
        "observations_stored": warehouse.count("stop_time_updates"),
    }


def _dashboard_payload(
    warehouse: Warehouse, settings: Settings, pool: ThreadPoolExecutor | None = None
) -> dict:
    """Everything the dashboard renders, in one round trip.

    The fourteen analytics are independent of each other and every one of them
    re-derives the same "latest observation per stop" view, so run sequentially
    they simply add up: measured at 2.1M rows, ~0.9s each for a 7s payload
    against a 10s push interval.

    They are run concurrently instead. Warehouse is thread-affine, so each
    worker gets its own connection and PostgreSQL parallelises the work rather
    than serialising it behind one cursor.
    """
    jobs = {
        "summary": lambda: _summary(warehouse, settings),
        "timeseries": lambda: analytics.delay_timeseries(warehouse, bucket_minutes=5, hours=6),
        "stations": lambda: analytics.station_delays(warehouse, limit=12, min_observations=2),
        "categories": lambda: analytics.category_breakdown(warehouse),
        "distribution": lambda: analytics.delay_distribution(warehouse),
        "network": lambda: analytics.network_snapshot(warehouse, limit=400),
        "positions": lambda: analytics.live_positions(warehouse, limit=POSITION_LIMIT),
        "history_window": lambda: analytics.history_window(warehouse),
        "polls": lambda: analytics.recent_polls(warehouse, limit=60),
        "cancellations": lambda: analytics.cancellations(warehouse),
        "skipped_stations": lambda: analytics.skipped_stations(warehouse, limit=8),
        "worst_trips": lambda: analytics.worst_trips(warehouse, limit=8),
        "origination": lambda: analytics.delay_origination(warehouse, limit=10, min_steps=3),
        "delay_minutes": lambda: analytics.station_delay_minutes(warehouse, limit=12),
    }

    payload: dict[str, Any] = {
        "generated_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "rules": _rules(),
    }

    if pool is None:   # Tests and direct callers: sequential is fine and leaks nothing.
        for key, fn in jobs.items():
            payload[key] = fn()
        payload["positions_capped"] = len(payload["positions"]) >= POSITION_LIMIT
        return payload

    futures = {pool.submit(fn): key for key, fn in jobs.items()}
    for future in as_completed(futures):
        payload[futures[future]] = future.result()
    payload["positions_capped"] = len(payload["positions"]) >= POSITION_LIMIT
    return payload
