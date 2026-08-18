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

---

## 4. Test-driven development

Tests live in `tests/`, separate from the package, and were written **before**
each implementation. The cycle throughout:

1. Write tests describing the behaviour, including the edge cases.
2. Run them and watch them fail for the right reason.
3. **Commit the failing tests**, so the red state is in the history.
4. Implement the smallest thing that passes.
5. Run, confirm green, commit with the passing count in the message.

The git history reads as alternating `test(...)` / `feat(...)` commits, each
naming its red or green state.

### Layered by cost

| Marker | Needs | Count | Run in CI |
|---|---|---|---|
| *(none)* | Nothing | 100 | Yes |
| `postgres` | A reachable database | 45 | Yes (service container) |
| `network` | The live upstream feed | 7 | No |

152 tests total, against 1,492 lines of package code — close to a 1:1
test-to-source ratio.

The `network` exclusion is deliberate: CI must not fail because Deutsche Bahn is
having a bad morning. Run locally, those seven tests are the only thing that
would catch a moved endpoint, a GTFS-RT version bump, or `trip_id`s that stop
joining against the timetable — failures no fixture can simulate.

### Real bytes, not synthetic protobufs

`tests/fixtures/gtfs_rt_sample.pb` is 25 genuine long-distance trip updates plus
three service alerts, carved from a live capture by `scripts/make_fixture.py`
(9 KB, versus 45 MB for the full feed). Testing a decoder against protobufs
written by the same mental model that wrote the decoder proves very little; these
are the actual bytes Deutsche Bahn publishes.

---

## 5. Three bugs the tests caught

These are the reason the process was worth following.

### 5.1 Insert counts silently wrong beyond 1,000 rows

The first live run reported `1,380 rows written` for 129 trains — about 10 stops
each, which looked plausible. The database disagreed:

```
rows actually in table : 1,344
rows reported written  :   344
```

`psycopg2.extras.execute_values` paginates internally, and `cursor.rowcount`
reflects only the **final page**. With `page_size=1000` and 1,344 rows, the
reported count was the 344-row remainder.

The data was never wrong — only the number. But that number feeds the ingestion
audit trail and the dashboard's throughput panel, so every operational metric
would have understated reality by whatever the last page happened to be.

Unit tests had missed it because they inserted two rows. The regression test
inserts 1,200 to cross the page boundary, and paging is now driven explicitly and
summed:

```python
for start in range(0, len(rows), _PAGE_SIZE):
    execute_values(cur, _INSERT_STU, rows[start:start + _PAGE_SIZE], page_size=_PAGE_SIZE)
    written += cur.rowcount
```

### 5.2 The model was 56% worse than doing nothing

The first delay model predicted the **absolute** delay at the next stop. On
synthetic data it beat the baseline. On real data:

```
model MAE            : 133.0 s
persistence baseline :  85.0 s
improvement          : -56.4 %
```

Rather than tune it, the actual delay dynamics were measured:

```
stop-to-stop delta: median 0 s, 55.5% exactly zero
delay itself      : sigma 782 s
correlation(delay, delta) : -0.149
```

That explains the failure completely. Persistence — "assume the delay carries
over" — is *exactly right* 55% of the time. Predicting the absolute next delay
forces the learner to reconstruct a quantity with a standard deviation of 782
seconds in order to beat an error of 85. It was never going to.

Two changes, both dictated by the measurement:

- **Predict the delta**, not the absolute delay. The model learns only the
  correction; the large known quantity is added back afterwards.
- **Use `absolute_error` loss**, fitting the conditional median. With a median
  delta of zero, the model can answer "no change" — correct for most stop pairs.
  Squared error chases the long tail and is dragged off zero.

```
before : -56.4 %   (absolute target, squared error)
after  :  -2.0 %   (delta target, MAE loss, same 30 minutes of data)
later  :  +4.4 %   (same model, more history collected)
```

The fixture was rewritten to reproduce the real distribution — 55% zero deltas —
so the test now fails the way production did rather than on a tidy synthetic
curve.

### 5.3 The same station ranked twice

The dashboard showed **Göttingen** twice in "worst stations", with different
numbers. Not a rendering fault:

```
stop_id  stop_name  parent_station
22052    Göttingen  (none)
183998   Göttingen  22052
203496   Göttingen  22052
...      Göttingen  22052        -- 7 stop_ids in total
```

GTFS models every platform as its own stop. Grouping by `stop_id` ranks
platforms, splitting one station's traffic across several rows — each showing a
fraction of the real picture. Station-level analysis now rolls up to
`COALESCE(parent_station, stop_id)`.

---

## 6. Design decisions worth recording

### The fact table is append-only, keyed on the feed timestamp

```sql
PRIMARY KEY (trip_id, service_date, stop_sequence, feed_timestamp)
```

Storing only the latest state per stop would make "how late is this train?"
answerable and "how did this delay develop?" impossible — and delay development
is what propagation analysis and the ML model are made of.

Putting `feed_timestamp` in the key also makes idempotency a database property
rather than application logic: `ON CONFLICT DO NOTHING` means re-ingesting an
unchanged feed writes nothing, so the collector can crash, restart, or replay
without corrupting history.

The `current_stop_delays` view recovers latest-state via `DISTINCT ON`, so
"right now" queries stay cheap.

### Punctuality uses DB's own 6-minute threshold

`PUNCTUALITY_THRESHOLD_SECONDS = 360`. Deutsche Bahn counts a stop as *pünktlich*
when it is under six minutes late. Inventing a threshold would produce numbers
that look like DB's published statistics but cannot be compared with them.

### Missing is not zero

A stop with no prediction decodes to `None`, never `0`. Collapsing the two would
silently count every unknown stop as perfectly on time and inflate punctuality.
A test pins this.

### Delay is arrival, falling back to departure

`COALESCE(arrival_delay, departure_delay)` — a trip's first stop has no arrival
and its last has no departure. Using arrival alone would discard every origin
station.

### The collector never dies

Any exception in a poll cycle is caught, recorded in `feed_polls`, and returned
in the summary. A collector that exits on a bad upstream response loses data for
as long as nobody is watching, and upstream outages arrive as HTML error pages
served with `200 OK` — which is why `decode_feed` raises a clear `ValueError`
rather than letting a protobuf `DecodeError` escape.

### ETags matter more than usual here

The open fallback payload is ~45 MB. A `304 Not Modified` turns a poll into a few
hundred bytes. Measured: ~50 MB and ~17 s per changed poll.
