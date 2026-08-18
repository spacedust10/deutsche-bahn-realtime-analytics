"""Dashboard backend: REST analytics plus a WebSocket that pushes live state.

The WebSocket exists because the dashboard is a *realtime* view: polling six
REST endpoints on a timer from the browser would triple the query load and
still lag the collector. One push carries the whole dashboard payload.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import analytics
from .config import Settings
from .ml import DelayModel
from .storage import Warehouse

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "delay_model.joblib"

PUSH_INTERVAL_SECONDS = 5


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    # ponytail: one shared autocommit connection. Fine for a single-process
    # dashboard; swap for a psycopg2 ThreadedConnectionPool if this ever fans
    # out to multiple workers.
    state: dict[str, Any] = {"warehouse": None, "model": None}

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
        if state["warehouse"] is not None:
            state["warehouse"].close()

    app = FastAPI(title="DB Fernverkehr Realtime Analytics", version="0.1.0", lifespan=lifespan)

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
    def propagation(trip_id: str) -> list[dict]:
        rows = analytics.delay_propagation(warehouse(), trip_id=trip_id)
        if not rows:
            raise HTTPException(status_code=404, detail=f"no observations for trip {trip_id}")
        return rows

    @app.get("/api/model")
    def model_info() -> dict:
        model = state["model"]
        if model is None:
            return {"trained": False, "detail": "no model on disk; run scripts/train_model.py"}
        return {"trained": True, **model.metrics}

    # --- WebSocket ---------------------------------------------------------

    @app.websocket("/ws")
    async def live(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                payload = await asyncio.to_thread(_dashboard_payload, warehouse(), settings)
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


def _dashboard_payload(warehouse: Warehouse, settings: Settings) -> dict:
    """Everything the dashboard renders, in one round trip."""
    return {
        "generated_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "summary": _summary(warehouse, settings),
        "timeseries": analytics.delay_timeseries(warehouse, bucket_minutes=5, hours=6),
        "stations": analytics.station_delays(warehouse, limit=12, min_observations=2),
        "categories": analytics.category_breakdown(warehouse),
        "distribution": analytics.delay_distribution(warehouse),
        "network": analytics.network_snapshot(warehouse, limit=400),
        "worst_trips": analytics.worst_trips(warehouse, limit=8),
    }
