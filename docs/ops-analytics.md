# Operating the dashboard

What each panel is for, and what the data says to build next. Figures below are
from the live warehouse on 2026-08-26 and are there to justify the ranking, not
to be maintained.

## The KPI tree

Punctuality is the published number, but nothing can be *done* to a percentage.
Each level below it is one question closer to an action.

```
Punctuality (share of calls under 6 min late)
├── How late, and for whom      → delay distribution · median vs 90th percentile
├── Where the minutes are       → late minutes per station, cumulative share
├── Who makes them              → delay created at a station vs carried in
│   └── on which link           → time lost or recovered per segment (map)
├── When                        → punctuality by weekday × local hour
└── What it will be shortly     → delay model vs persistence baseline
```

## Panel → decision

| Panel | Question | Decision it feeds |
|---|---|---|
| Where delay is created | Is this station making delay or inheriting it? | Where to send an intervention: dwell, platform reoccupation, junction conflict |
| Where the minutes are | How much of the network's lateness sits here? | Which handful of nodes a fix could actually move |
| Typical call vs bad call | Is this station consistently late or occasionally terrible? | Whether to fix a process or manage a tail: connection buffers, passenger information |
| When the network fails | Which hours break, in the clock the timetable is written in? | Crew, stock and recovery-time placement |
| Live network map | Which links lose time, which give it back? | Where timetable padding is working, and where it is being eaten |
| Delay model | Will this train still be late at its next stop? | Whether to hold a connection now |

Mean delay per station stays on the page, but it is a symptom: it counts every
train that arrives late, including one that lost its time 300 km earlier. The
origination panel exists because that number cannot be acted on.

## What to build next, ranked by decision value

1. **Connection risk at hubs.** Every panel here is about trains; passengers
   miss journeys at transfers. The data is already warehoused: `stop_times`
   holds scheduled arrivals and departures at a shared station, and the fact
   table holds the delay on both legs. Nothing yet joins them. This is the
   largest gap between what is collected and what is answered.
2. **Cause tagging from the service-alerts feed.** Every panel says *where* and
   *when*, none says *why*, so no finding can be attributed to infrastructure,
   crew, or knock-on. DB publishes a GTFS-RT service-alerts stream and
   `config.py` already resolves its URL, but `gtfs_rt.py` decodes TripUpdates
   only and drops alert entities. One endpoint away from being answerable.
3. **Ingestion continuity.** The warehouse holds 3.35M observations across 7
   service dates but only 61 distinct hours: the collector was down for five
   days between 21 and 26 August and nothing recorded that it had stopped. Every
   weekly conclusion inherits that hole. The dashboard shows feed health per
   poll; it does not show absence.
4. **Segment and hour coverage.** 255 links carry observations against 4,254 in
   the timetable, and 66 of 168 weekly hour-cells are filled, some on two calls.
   The map and the heatmap are honest about being sparse, but the fix is
   collection time, not code, and it gates points 1 and 5.
5. **A model worth acting on.** Cross-validated MAE is 193.5 s against a
   195.5 s persistence baseline: 1% better than assuming the delay simply
   persists. On this feature set that is what should be expected. The features
   that would plausibly beat persistence are the ones above: which link the
   train is about to run, how loaded the junction ahead is, and what the alerts
   feed says is wrong.

Deliberately not on this list: passenger-weighted delay. GTFS-RT carries no
loading data, so any weighting would be invented and would quietly become the
number people quote.
