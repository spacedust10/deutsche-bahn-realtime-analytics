# How This Project Was Built

A complete account of the work: how the data source was identified and verified,
every technology choice and the reason for it, the test-first process, the bugs
that process caught, and the full reference list.

---

## 1. Reading the brief

The starting point was a single file, `architecture.md`, specifying a pipeline:

> Deutsche Bahn → DB Fernverkehr (ICE/IC/EC) → official GTFS-RT feed → Protocol
> Buffers → Python collector → PostgreSQL → historical analysis + machine learning

Four constraints shaped everything that followed:

1. **Deutsche Bahn's own feed, not an aggregator.** The document is explicit that
   this "is different from GTFS.DE", which aggregates many German operators.
2. **Long-distance only** — ICE, IC, EC.
3. **GTFS-Realtime over Protocol Buffers**, not scraping.
4. **PostgreSQL as the historical store**, feeding analysis and ML.

The delivery brief added a live animated dashboard, a separate test suite built
test-first, and this document.

---

## 2. Finding the real data source

This was the first task and the one worth doing carefully. A pipeline built on a
guessed endpoint is worthless, so nothing was written until a feed had been
fetched and decoded.

### 2.1 Locating the official endpoint

Searching Deutsche Bahn's developer documentation led to
[developer-docs.deutschebahn.com — GTFS und GTFS-RT](https://developer-docs.deutschebahn.com/doku/datenstroeme/stroeme-gtfs-10582270),
which documents the DB Fernverkehr streams:

```
https://gtfs-datenstroeme.tech.deutschebahn.com/db-fernverkehr/gtfs.zip
https://gtfs-datenstroeme.tech.deutschebahn.com/db-fernverkehr/gtfsrt_trip_updates.proto
https://gtfs-datenstroeme.tech.deutschebahn.com/db-fernverkehr/gtfsrt_service_alerts.proto
```

Authentication is a `DB-Api-Key` header. The documentation recommends polling the
static feed every 5 minutes and the realtime feed every 20 seconds, both with an
ETag, and states the realtime stream has a 24-hour horizon while the static
timetable covers at least 30 days ahead. Access is arranged via
`ris-gtfs@deutschebahn.com`.

Verified directly:

```
$ curl -o /dev/null -w "%{http_code}" \
    https://gtfs-datenstroeme.tech.deutschebahn.com/db-fernverkehr/gtfsrt_trip_updates.proto
401
```

The endpoint is real and key-gated exactly as documented. That `401` is now
asserted by a test (`tests/test_live_feed.py`), so if DB ever opens or moves the
endpoint, the suite says so.

### 2.2 The credentials problem, and the honest answer

Obtaining a DB API key is a manual request process. Building a repository that
cannot run for anyone without credentials is not a useful deliverable, and
silently substituting a different source would misrepresent the data.

The resolution: **both sources are implemented behind one interface, and the
active one is reported everywhere** — in the collector log, in `feed_polls`, in
the API response, and in the dashboard header.

`Settings.realtime_source()` returns a `Source` carrying `official: bool`. Set
`DB_API_KEY` and it resolves to DB's endpoint with the auth header; leave it
empty and it resolves to the open feed. Nothing downstream changes, because the
decoding, filtering, storage and analysis paths are identical.

### 2.3 The open fallback

[gtfs.de](https://gtfs.de/en/realtime/) publishes a free GTFS-RT stream and
matching static timetables, derived from the DELFI e.V. NeTEx dataset under
CC BY-SA 4.0:

```
https://realtime.gtfs.de/realtime-free.pb            # updated every 10 seconds
https://download.gtfs.de/germany/fv_free/latest.zip  # long-distance timetable
```

The realtime stream covers *all* German public transport, which is precisely what
the architecture says to avoid. That objection is answered by the filter rather
than by the source: the
[long-distance static feed](https://gtfs.de/en/feeds/de_fv/) contains only ICE,
IC, EC, ECE, EN and RJ services, and joining realtime `trip_id`s against it
reduces the stream to long-distance traffic. The same join runs against the
official DB feed, so the scoping logic is not fallback-specific.

### 2.4 Verifying the join before writing any pipeline

The entire design rests on realtime `trip_id`s matching static `trip_id`s. That
was tested before a line of the collector existed:

```
feed header version : 2.0
feed entities       : 202,874  (92,664 trip updates, 110,210 alerts)
static long-distance trips : 5,658
intersection        : 122 currently-running long-distance trips
```

Then the join was checked for meaning, not just cardinality:

```
trip 1536569 -> ICE 11
  seq 0  Frankfurt(Main)Hbf   dep_delay    0
  seq 1  Fulda                arr_delay    0   dep_delay   60
  seq 2  Eisenach Hbf         arr_delay    0   dep_delay    0

trip 750650 -> ICE 41
  seq 0  Frankfurt(Main)Hbf   dep_delay   60
  seq 1  Hanau Hbf            arr_delay  180   dep_delay  240
  seq 2  Aschaffenburg Hbf    arr_delay  120   dep_delay  180

categories: ICE 109, IC 11, ECE 1, EC 1
```

Real trains, real stations, real delays, correctly scoped. Only then did
implementation begin.

---

## 3. Technology choices

Every dependency in `requirements.txt` earns its place. The architecture named
the stack; these notes record why each specific library, and what was rejected.

| Layer | Choice | Reasoning |
|---|---|---|
| Feed access | `requests` | One HTTPS GET with headers and an ETag. `httpx`/async buys nothing for a single sequential poll. |
| Decoding | `gtfs-realtime-bindings` | Google's official generated bindings. Hand-writing the `.proto` would mean maintaining a copy of a spec that upstream owns. |
| Storage | PostgreSQL + `psycopg2` | Named by the architecture. `execute_values` batching turns a ~1,400-row poll from thousands of round trips into one. |
| Analysis | SQL + `pandas` | Aggregation belongs next to the data; pandas only appears where the ML code needs frames. |
| ML | `scikit-learn` `HistGradientBoostingRegressor` | Tabular, few thousand rows, mixed numeric/categorical. Deep learning here would be ceremony. |
| API | FastAPI + `uvicorn` | Native WebSocket support and typed query validation — the out-of-range `bucket` rejection is free. |
| Charts | Apache ECharts 5.5.1, vendored | Built-in animated transitions between data states, which is the entire point of a realtime view. |
| Frontend | Vanilla JS | ~450 lines. A framework and build step would add more configuration than code. |
| Tests | `pytest` | Markers cleanly separate unit / database / network layers. |

### Choices deliberately *not* made

- **No ORM.** The analytics are aggregate SQL with `FILTER`, `percentile_cont`
  and `DISTINCT ON`. An ORM would obscure them and buy nothing.
- **No `python-dotenv`.** Reading `KEY=VALUE` is eight lines of standard library
  for a file format this project also authors.
- **No Redis / message queue.** One collector writing to one database. A queue
  would be infrastructure without a problem.
- **No CDN.** ECharts is vendored (`web/vendor/`, Apache-2.0). The dashboard must
  render where only the feed is reachable, and a CDN outage should not blank
  every chart.
- **No Alembic.** One `schema.sql` of `CREATE TABLE IF NOT EXISTS`, applied
  idempotently at startup.
