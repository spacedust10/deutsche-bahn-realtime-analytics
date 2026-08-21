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
| ML | `scikit-learn` `HistGradientBoostingRegressor` | Tabular, few thousand rows, mixed numeric/categorical. Deep learning here would be ceremony. Loss chosen by measurement, not preference — see §5.2c. |
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
| *(none)* | Nothing | 113 | Yes |
| `postgres` | A reachable database | 84 | Yes (service container) |
| `network` | The live upstream feed | 7 | No |

204 tests total, against ~1,970 lines of package code — close to a 1:1
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
before : -56.4 %   (absolute target, squared error, single split)
after  :  -2.0 %   (delta target, MAE loss, same 30 minutes of data)
```

The fixture was rewritten to reproduce the real distribution, so the test now
fails the way production did rather than on a tidy synthetic curve.

### 5.2b The reported number was itself unreliable

Retraining as history accumulated gave +4.4%, then +0.3%, then -2.0% on
comparable sample sizes. That spread is a single random hold-out split on ~1,400
pairs — noise being reported as signal.

Scoring moved to 5-fold cross-validation, with the baseline measured on the
identical folds, and the standard deviation across folds reported alongside the
mean. The shipped model is then refitted on all the data. Current figures:

```
MAE  112.0 s (± 11.8 across folds)  vs persistence 115.9 s   -> +3.4 %
RMSE 339.3 s                        vs persistence 340.3 s   -> +0.3 %
```

### 5.2c A structural ceiling, found while fixing the tests

Making the fixture match reality exposed something the metric alone hides.

When **more than half** of stop-to-stop deltas are exactly zero, the conditional
median delta *is* zero — so "no change" becomes the MAE-optimal prediction.
Persistence is then unbeatable on MAE by construction, not by being good. The
live feed sits at **49.8% zero deltas**, just under the line, which is why a
+3.4% MAE gain is achievable at all and why it is small.

Measured on a fixture pinned to exactly 50%:

```
MAE  : model 90.6 s vs persistence  87.1 s   ->  -4.0 %   (persistence optimal)
RMSE : model 119.4 s vs persistence 134.9 s  -> +11.5 %   (model clearly better)
```

The model does learn real structure — it simply cannot express that as an MAE
gain against a metric whose optimum is a constant. Both metrics are now
reported, and the test suite asserts the RMSE improvement, since that is the one
that reflects learned signal rather than the shape of the loss function.

Choosing the loss was decided by measurement rather than preference. On live
data, cross-validated:

```
absolute_error : MAE +3.9 %   RMSE  +0.1 %
squared_error  : MAE -23.2 %  RMSE  -3.5 %
```

`absolute_error` wins on both, so it stays.

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

---

## 7. The dashboard

### Time series first

The architecture asks for realtime analysis, so the largest panel is the one that
shows change over time: mean delay as a filled area, P90 as a dashed line, and
punctuality on a second axis. One glance answers whether the network is
recovering or degrading — which a table of current values cannot.

### Animation carries information, not decoration

Charts are created **once** and updated with `setOption`, so ECharts tweens
between states. Recreating them on each push would flicker and discard the
transition. When a bar grows, the movement itself is the signal that something
changed.

Delayed trains use `effectScatter` — a ripple, not a static dot — so severity is
visible before any number is read. KPI counters tween from their previous value
for the same reason.

`prefers-reduced-motion` disables all of it.

### One socket, not six pollers

The WebSocket pushes the whole dashboard payload every five seconds. Six REST
endpoints on browser timers would triple the query load and still lag the
collector.

The page nonetheless paints from `GET /api/dashboard` on load, then hands over to
the socket. This was added after a headless render exposed the flaw: every chart
sat blank until the first push, and stayed blank wherever WebSockets are blocked.

### The map is stations, not vehicles

*(Superseded in section 12. GTFS-RT still carries no coordinates, but positions
are now derived from the timetable rather than snapped to the last station.)*

GTFS-RT `TripUpdate` carries no coordinates — only `VehiclePosition` does, and
the long-distance feed does not publish it. Trains were therefore drawn at their
last reported station, which is honest about what the data supports.

131 trains alone read as scattered dots. Drawing all ~1,200 long-distance
stations behind them, dimmed, makes the panel legible as Germany's rail network.
Station geometry is static, so it is fetched once rather than riding along in
every push.

---

## 8. What the live data actually showed

From a single ~40-minute collection window on a weekday morning: **60,504
stop-time observations** across **152 distinct long-distance services**.

| Measure | Value |
|---|---|
| Punctuality (<6 min) | **77.0 %** |
| Mean delay | 5.5 min |
| Worst single delay | 91 min |
| Skipped station calls | 1.82 % of calls, 11 services |
| ICE | 75.7 % punctual, mean 6.0 min |
| IC | 85.3 % punctual, mean 2.0 min |
| ECE | 100 % punctual, mean 0.4 min |
| EC | 100 % punctual, mean **−0.4 min** — running early |

ICE, the flagship product, is the least punctual of the four. That is not a
data error: ICE services run the longest routes with the most intermediate
stops, so they have the most opportunity to accumulate delay.

The propagation view caught a real failure: an ICE running on time through
Hamburg and Lüneburg, then jumping to **+91 minutes at Uelzen** and holding that
delay for the rest of its route to Frankfurt. That shape — a step, not a ramp —
is exactly why 55% of stop-to-stop deltas are zero, and why the persistence
baseline is so hard to beat.

---

## 9. Limitations

Stated plainly, because a portfolio project that overclaims is worse than one
that does less.

- **The default source is the open feed, not DB's own.** The official path is
  implemented and verified as reachable and key-gated, but running it needs
  credentials. The active source is always displayed.
- **The model's margin is small, and partly capped by the metric.** +3.4% MAE
  over persistence (5-fold, ±11.8 s) on roughly an hour of history. With ~50% of
  deltas exactly zero, MAE improvement is structurally limited; RMSE is the
  honest place to look. Real gains need days of data, weather, infrastructure
  incidents and upstream-train state — none of which is in a GTFS-RT feed.
- **Positions are station-level.** No `VehiclePosition` in this feed, so there is
  no interpolation between stops.
- **Single-process.** One shared PostgreSQL connection, marked in the code. A
  connection pool is the upgrade if the API ever fans out to workers.
- **Service alerts are not ingested.** The endpoint is configured and stop-level
  `SKIPPED` cancellations are captured, but the alerts stream itself is not yet
  decoded.
- **Sample window is short.** The figures above are ~40 minutes of one weekday
  morning, not a trend. Punctuality varies enormously by hour, season and
  weather, and an early-morning window flatters the network.

### Next steps, in order of value

1. Run the collector for weeks — every number here improves with history, and the
   model most of all.
2. Ingest `gtfsrt_service_alerts.proto` for incident context.
3. Add upstream-train state as a feature; delay propagates *between* services,
   not just along one.
4. Partition `stop_time_updates` by month once it passes tens of millions of rows.

---

## 9b. Reproducing this

```bash
git clone https://github.com/spacedust10/deutsche-bahn-realtime-analytics
cd deutsche-bahn-realtime-analytics
pip install -r requirements.txt
```

The 94 tests that need neither PostgreSQL nor the network pass on a clean clone
with no configuration:

```bash
python3 -m pytest -m "not postgres and not network"
```

For the full pipeline, bring up PostgreSQL (`docker compose up -d`), then
`make db && make collect` in one shell and `make serve` in another. The
dashboard is at `http://127.0.0.1:8000` and starts showing live ICE/IC/EC
delays within one poll cycle. `make train` once some history exists.

No credentials are required for any of it; setting `DB_API_KEY` switches the
collector to Deutsche Bahn's own feed.

---

## 10. References

### Data sources

- **DB Fernverkehr GTFS / GTFS-RT (official, primary)** —
  https://developer-docs.deutschebahn.com/doku/datenstroeme/stroeme-gtfs-10582270
  Endpoints, `DB-Api-Key` authentication, polling intervals, feed horizons.
- **Deutsche Bahn Developer Portal** — https://developer-docs.deutschebahn.com/
- **gtfs.de realtime feed (open fallback)** — https://gtfs.de/en/realtime/
  `https://realtime.gtfs.de/realtime-free.pb`, CC BY-SA 4.0, 10-second updates.
- **gtfs.de long-distance timetable** — https://gtfs.de/en/feeds/de_fv/
  `https://download.gtfs.de/germany/fv_free/latest.zip`, ICE/IC/EC/ECE/EN/RJ.
- **DELFI e.V.** — https://www.delfi.de/ — the NeTEx dataset the open feeds derive from.

### Standards

- **GTFS Realtime reference** — https://gtfs.org/documentation/realtime/reference/
  `TripUpdate`, `StopTimeUpdate`, `ScheduleRelationship` semantics.
- **GTFS Realtime `.proto`** — https://gtfs.org/documentation/realtime/proto/
- **GTFS Schedule reference** — https://gtfs.org/documentation/schedule/reference/
  `routes.txt`, `trips.txt`, `stops.txt`, `parent_station`.
- **Google Transit — GTFS Realtime** — https://developers.google.com/transit/gtfs-realtime
- **Protocol Buffers** — https://protobuf.dev/

### Libraries

- **gtfs-realtime-bindings** — https://github.com/MobilityData/gtfs-realtime-bindings
- **psycopg2 — `execute_values`** — https://www.psycopg.org/docs/extras.html
- **PostgreSQL `DISTINCT ON`** — https://www.postgresql.org/docs/current/sql-select.html
- **PostgreSQL aggregate `FILTER`** — https://www.postgresql.org/docs/current/sql-expressions.html
- **scikit-learn `HistGradientBoostingRegressor`** — https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingRegressor.html
- **FastAPI WebSockets** — https://fastapi.tiangolo.com/advanced/websockets/
- **FastAPI lifespan events** — https://fastapi.tiangolo.com/advanced/events/
- **Apache ECharts** — https://echarts.apache.org/en/option.html
- **pytest markers** — https://docs.pytest.org/en/stable/example/markers.html

### Domain

- **DB punctuality definition** — https://www.deutschebahn.com/de/konzern/im_blickpunkt/puenktlichkeit-6878476
  The under-6-minutes threshold used throughout.
- **DB Open Data portal** — https://data.deutschebahn.com/

---

## 11. Technology summary

| Area | Technology |
|---|---|
| Language | Python 3.13 |
| Realtime standard | GTFS-Realtime 2.0 |
| Wire format | Protocol Buffers |
| Feed transport | HTTPS with ETag conditional requests |
| Decoding | gtfs-realtime-bindings, protobuf |
| HTTP client | requests |
| Database | PostgreSQL 15 |
| Driver | psycopg2 (`execute_values` batching) |
| Analysis | SQL, pandas, numpy |
| ML | scikit-learn (HistGradientBoostingRegressor), joblib |
| API | FastAPI, uvicorn, WebSockets |
| Frontend | Vanilla JS, Apache ECharts 5.5.1 (vendored), CSS Grid |
| Notebook | Jupyter, matplotlib, seaborn |
| Tests | pytest (204 tests, layered by marker) |
| CI | GitHub Actions with a PostgreSQL service container |
| Local infra | Docker Compose, Makefile |

---

## 12. Second iteration: Stellwerk

The first build worked but had three problems: it wore Deutsche Bahn's
trademark, its map showed trains parked at stations, and its charts broke two
rules of data visualisation that matter.

### 12.1 The identity was a liability

The dashboard's mark was the letters **DB** in Deutsche Bahn's brand red. On a
public portfolio repository that reproduces a registered trademark and implies
official affiliation with an operator that has not endorsed anything here.

It was also a colour problem. In a delay dashboard red has to mean *late*. A
brand red sitting in the header competes with the severity scale for the same
meaning, and the reader has to learn which red is which.

Both are fixed by the same change. The project is now **Stellwerk**, the German
word for the signal box that watches and routes a rail network, drawn as a track
turnout with a signal showing clear. Brand red survives only as a CSS token
reserved for identity, never applied to data.

### 12.2 Colour was computed, not chosen

Delay is a magnitude, so it wants a sequential scale. The intuitive choice
(green to red) is multi-hue, which is normally forbidden because a rainbow ramp
loses its order under colour-vision deficiency. Semantic heat is the documented
exception, on one condition: the order has to survive without hue.

That means monotone lightness. The first attempt failed exactly as predicted:

```
[FAIL] CVD separation   worst all-pairs #d55181 vs #199e70  dE 1.6 (deutan)
[FAIL] Normal-vision    worst all-pairs #e66767 vs #d95926  dE 7.1
```

Those four colours were all sitting in the same lightness band, so under deutan
simulation two of them collapsed into each other. The fix was to solve for the
steps rather than pick them: target an OKLCH lightness ladder, then search for
the most saturated hex at each rung.

| Band | Hex | OKLCH L | Contrast on panel |
|---|---|---|---|
| On time (<3 min) | `#007a2a` | 0.505 | 3.15:1 |
| 3 to 6 min | `#a28500` | 0.626 | — |
| 6 to 15 min | `#ff7406` | 0.715 | — |
| 15 min or more | `#ff9c9c` | 0.794 | — |

```
[PASS] Lightness monotone   steps read light->dark
[PASS] Adjacent dL          all gaps >= 0.06
[PASS] Light-end contrast   #007a2a at 3.15:1 vs surface
[FAIL] Single hue           hue spread 119deg
```

The remaining failure is the semantic-heat exception itself, and its condition
is a scale legend, which the map ships. Greyscale the map and the severity order
is still readable, which was the whole point.

One collision survived into the browser and was caught on screen: the **EC**
series colour was the same hex as the status "good" green, so the EC bar read as
a verdict rather than an identity. EC moved to violet, which also improved
separation from its neighbours (worst adjacent CVD dE 8.4 to 26.0).

Muted body text failed too. The slate `#6b7c93` measured **4.1:1** on the panel
surface, under the 4.5:1 floor. This is the most common contrast failure there
is: grey text on a tinted dark ground, chosen because it looks elegant. Lifted
to `#8195ad` (5.69:1).

### 12.3 Two charts were lying

**The delay chart was dual-axis.** It plotted mean delay in minutes and
punctuality as a percentage on one plot with two scales. Where the two lines
cross is an artefact of how the axes were aligned, not a fact about trains, and
readers infer a relationship from it anyway. Punctuality now has its own chart.

**The station bars were coloured by their own value.** Bar length already
encodes delay; colouring the bars by the same number spends the identity channel
re-encoding what the reader can already see. One hue now.

### 12.4 The map became a real map

Trains sat on their last reported station because `TripUpdate` carries no
coordinates. But the static timetable carries `stop_times`, and a delay is a
shift applied to a schedule. That is enough to place a train:

1. Load `stop_times` into PostgreSQL (55,099 scheduled calls).
2. For each call, actual time = scheduled time + the delay observed there.
3. Find the segment whose window contains the requested instant.
4. Interpolate along it, and take the bearing from the two station coordinates.

`ICE 11` resolves to 48.334 N, 10.970 E, 14.6 % of the way from Augsburg Hbf to
München-Pasing, heading 119 degrees. Roughly 300 trains resolve at once.

The network underneath is derived rather than sourced: `LEAD()` over
`stop_times` pairs consecutive calls, and `DISTINCT` collapses the hundreds of
trips sharing each piece of track into **1,910** unique links.

Rendering is **MapLibre GL JS** (BSD-3-Clause, vendored) over **OpenFreeMap**
vector tiles (ODbL, no API key). The library is vendored like ECharts; the tiles
cannot be, so the map degrades to network-and-trains on a dark ground when
tiles are unreachable.

MapLibre 5.x is pinned deliberately: 6.x is ESM-only and drops the UMD bundle a
plain `<script>` tag needs.

**Positions do not tween.** A GeoJSON source snaps to its new coordinates, so
trains would teleport every ten seconds. Displayed positions are interpolated
toward their targets on `requestAnimationFrame` across the push interval, which
is what makes them glide. Under `prefers-reduced-motion` they snap instead.

**The time slider** replays history. Scrubbing calls `/api/positions?at=...`,
which considers only observations published at or before that instant, so
replaying 03:00 shows what was known at 03:00 rather than back-dating later
corrections.

### 12.5 Ten seconds, measured rather than assumed

The upstream stream republishes every 10s, so the obvious reading is "poll every
10s". Measuring first changed the answer:

```
transfer = 43,668,650 bytes    time = 9.29s    encoding = none
```

No gzip. 43.7 MB per fetch, and the download alone takes 9.3 s, so a 10 s poll
means downloading continuously at ~4.4 MB/s, about **377 GB/day**, against a
donation-funded community server.

The dashboard is what needs to feel live, and it does: the browser refreshes on
a 10 s heartbeat, matching the feed's own rhythm. The collector polls at 30 s,
and the header shows a **data age** counter that ticks every second, so the page
never implies data is fresher than it is. With a DB API key the official
long-distance feed is far smaller and a true 10 s fetch becomes reasonable.

### 12.6 The heartbeat did not fit

The 10 s push was set, and the browser silently fell back to polling. The
payload was taking **8 to 18 seconds** to build, so the socket loop never
finished a cycle. Two independent causes:

**An unbounded query.** Every analytic reads `current_stop_delays`, a
`DISTINCT ON` over the whole fact table with no time bound, so cost grew with
collected history. An index matching the view's `ORDER BY` exactly let it
resolve by index scan instead of sorting 417k rows.

| Query | Before | After |
|---|---|---|
| punctuality | 0.37 s | 0.16 s |
| stations | 0.83 s | 0.15 s |
| categories | 0.83 s | 0.19 s |
| cancellations | 0.89 s | 0.22 s |
| **total** | **6.3 s** | **2.0 s** |

**One connection, several threads.** A single psycopg2 connection served both
FastAPI's threadpool and the WebSocket's `asyncio.to_thread` calls. psycopg2
serialises concurrent users of one connection, which is where the 8-to-18 second
spread came from. `Warehouse` is now thread-affine.

End to end: **18.3 s to 1.75 s**, and stable across repeats.

### 12.7 What the browser caught that the tests did not

Every test passed while these were broken. They were all found by opening the
page.

- **Delay propagation plotted every stop at zero.** The endpoint returns
  `arrival_delay` and `departure_delay`; the chart read `delay_seconds`. Every
  lookup was `undefined`, coerced to `0`, and rendered as a perfectly punctual
  train. A service the table showed at +124 min drew as a flat line on zero.
  Nulls are now dropped rather than plotted, because a call with no prediction
  is not an on-time call.
- **A column that was always empty.** The worst-services table showed "Last
  reported" from a payload that carries a call count, not a station.
- **The map ate the page scroll.** Wheeling over the map zoomed it instead of
  scrolling past, trapping the reader. Fixed with cooperative gestures.
- **Two stranded grid items.** Six metrics in an auto-fit grid resolved to five
  columns and left one tile alone on its own row; three panels in a two-column
  row stranded the third the same way.
- **An instruction that was not true.** The model panel's empty state named a
  command that did not exist. It now names `make train`, which does.

### 12.8 Two collectors, quietly doubling the load

The ingestion panel reported alternating polls of ~2,050 rows and **0 rows**,
and poll spacing that wandered between 10 and 39 seconds instead of holding at
30. Both had the same cause: two collector processes were running, one left
over from an earlier session.

Nothing corrupted, which is the point. The append-only fact table is keyed on
`(trip_id, service_date, stop_sequence, feed_timestamp)`, so the second process
fetching the same feed content inserted nothing at all. `ON CONFLICT DO NOTHING`
absorbed the duplication exactly as designed, and the 0-row polls in the chart
are that guarantee being visible rather than a fault.

What it did cost was bandwidth: two processes pulling ~40 MB each meant double
the traffic against a donation-funded server, for zero additional data.

The lesson is about the check, not the pipeline. The process check used to
decide whether a collector was already running was `pgrep -f "python3 -m dbrt$"`,
which silently matched nothing, and a false negative from a liveness check reads
exactly like "nothing is running".

### 12.9 The model still loses, and says so

`HistGradientBoostingRegressor` exposes no `feature_importances_`, so the
importance panel had been built against a field the backend never produced.
Importance is now measured by permutation and normalised to shares.

Trained on the collected history, the honest result:

| Measure | Model | Persistence baseline |
|---|---|---|
| MAE | 159.7 s | 159.0 s |
| RMSE | 354.0 s | 352.9 s |

The model is **0.4 % worse than assuming nothing changes**. Section 5.2c already
established why: about half of stop-to-stop delay deltas are exactly zero, and
above that rate "no change" is the MAE-optimal prediction by construction.

The panel reports this in both directions rather than only when the model wins.
A metric that can only deliver good news is advertising, not measurement.

---

## 13. Technology added in this iteration

| Area | Technology |
|---|---|
| Map rendering | MapLibre GL JS 5.24.0 (BSD-3-Clause, vendored) |
| Basemap tiles | OpenFreeMap vector tiles (ODbL, no API key) |
| Basemap data | OpenStreetMap contributors |
| Colour space | OKLCH, validated for CVD separation and contrast |
| Feature importance | scikit-learn `permutation_importance` |

---

## 14. Third iteration: the motion audit

A pass over the frontend against Emil Kowalski's design-engineering rules. Most
findings were the ones that never announce themselves.

### 14.1 Two animation bugs that were not taste calls

**Trains moved at different speeds on different machines.** The map lerps each
train toward its new position on `requestAnimationFrame` with a fixed per-frame
factor. That is frame-rate dependent: at 120Hz it runs twice as many steps per
second as at 60Hz, so the same data animated at two different speeds depending
on the display. Now normalised against elapsed time.

**The refresh animation was never configured.** ECharts applies
`animationDuration` to the first render only; every later `setOption` uses
`animationDurationUpdate`. Only the former was set, so the animation users
actually see (a refresh every 10s) had been running on library defaults the
whole time. Both are set now, both under the 300ms ceiling.

### 14.2 Craft details

| Was | Is | Why |
|---|---|---|
| `.skip-link` transitions `top` | `transform: translateY()` | Layout properties cannot reach the GPU |
| Live dot animates `box-shadow` spread | `::after` ring on transform + opacity | It runs forever on an always-visible element; box-shadow repaints every frame |
| Buttons nudge 1px on press | `scale(0.97)`, transform in the transition | The press has to be felt; without transform in the transition the release snapped |
| Hover states ungated | `@media (hover: hover) and (pointer: fine)` | Touch fires hover on tap and leaves it stuck |
| Map popup: instant, centre origin, inline styles | 160ms scale from 0.96, per-anchor origin, in CSS | A popover scales out of its trigger, not its own middle |
| Reduced motion killed every transition | keeps opacity and colour, drops movement | Reduced motion means gentler, not absent; colour cues never caused motion sickness |

Deliberately not added: a page-load stagger. A dashboard loads into a task, and
choreographing six tiles on every refresh is decoration the reader pays for
repeatedly.

### 14.3 What the audit found underneath

Looking closely at the frontend surfaced three defects that had nothing to do
with motion.

**The map was mostly ghosts.** `live_positions` had no lower time bound, so
every trip ever collected stayed parked at its terminus. Of 1,200 markers,
**1,108 were trains from previous service dates** and only 82 were running. The
window is now bounded on both sides, with a short grace period so a train does
not blink out the moment it terminates: 1,200 markers became 115 real ones.

**The propagation chart drew a sawtooth.** A `trip_id` repeats every service
day, and the query ordered by `stop_sequence` alone, interleaving runs: seq 0
yesterday, seq 0 today, seq 1 yesterday. ICE 50 oscillated between +250 and 0
minutes. It now traces the newest run, or an explicit date passed from the table
row, and reads as the genuinely late service it is: 241, 275, 290, 263, ... 259.

**A cap was being displayed as a measurement.** The "trains live" metric read
`positions.length`, which is a limit, not a count. The API now reports whether
the list was truncated, and the metric matches its own label.

### 14.4 Latency, again

The payload had drifted from 1.75s back to **7.0s** against a 10s push, because
all twelve analytics re-derive the same `current_stop_delays` view and ran
sequentially in one thread. `EXPLAIN` showed the view itself costs only 234ms;
the problem was paying it twelve times in series.

They now run on a bounded, long-lived worker pool: **7.0s to ~4.0s**.

The first attempt at that pool was a bug worth recording. Creating a
`ThreadPoolExecutor` per request, with thread-affine connections, opens a fresh
connection per worker per request and never returns them. PostgreSQL hit its
100-client limit within a few refreshes and the test suite started skipping with
`sorry, too many clients already`. One pool for the process lifetime fixes it:
reused threads mean reused connections.

The remaining ~4s is still twelve queries deriving the same view. The real fix
is a materialised view refreshed by the collector, which would also need every
seeding test to refresh it; that is left as the next step rather than rushed.

### 14.5 Tests that expire

Fifteen tests failed for a reason unrelated to any change: they seeded fixtures
at a hardcoded `2026-08-18` while every analytic filters on `now() - interval`.
They passed on the day they were written and silently began returning nothing
once that date rolled out of the 24-hour window.

Fixtures are now anchored to `now()`. The tests were always about relative
recency; the calendar date was never the point, and pinning it made them a
time bomb with a three-day fuse.
