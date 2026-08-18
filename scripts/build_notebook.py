"""Generate notebooks/historical_analysis.ipynb.

The notebook is generated rather than hand-edited so its cells stay reviewable
in git as ordinary Python instead of a JSON blob with embedded outputs.
"""
import sys
from pathlib import Path

import nbformat as nbf

OUT = Path(__file__).resolve().parent.parent / "notebooks" / "historical_analysis.ipynb"

def MD(text):
    return nbf.v4.new_markdown_cell(text.strip())


def CODE(src):
    return nbf.v4.new_code_cell(src.strip())

CELLS = [
    MD("""
# DB Fernverkehr — Historical Delay Analysis

Offline analysis of the realtime history collected by `python -m dbrt`, covering the
four analyses named in `architecture.md`:

1. **Train punctuality** — against DB's own <6-minute definition
2. **Station-level delay** — where the network loses time
3. **Delay propagation** — how delay accumulates along a route
4. **Route reliability** — which products and corridors hold their schedule

The final section trains the delay-prediction model and checks it against a persistence baseline.
"""),
    CODE("""
import os, sys
sys.path.insert(0, os.path.abspath(".."))

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from dbrt import analytics
from dbrt.config import Settings
from dbrt.storage import Warehouse

sns.set_theme(style="darkgrid", palette="rocket")
plt.rcParams.update({"figure.figsize": (11, 4.5), "figure.dpi": 110, "axes.titleweight": "600"})

warehouse = Warehouse(Settings.from_env().dsn())
print(f"{warehouse.count('stop_time_updates'):,} stop-time observations stored")
"""),
    MD("## 1. Punctuality\n\nDeutsche Bahn counts a stop as punctual when it is less than 6 minutes late."),
    CODE("""
summary = analytics.punctuality(warehouse)
pd.Series(summary).to_frame("value")
"""),
    CODE("""
distribution = pd.DataFrame(analytics.delay_distribution(warehouse))
ax = sns.barplot(distribution, x="band", y="stops", hue="band", legend=False)
ax.set(title="Where every observed stop lands", xlabel="", ylabel="stop observations")
plt.xticks(rotation=25, ha="right"); plt.tight_layout(); plt.show()
"""),
    MD("## 2. Station-level delay\n\nStations ranked by mean delay, filtered to those with enough observations to be meaningful."),
    CODE("""
stations = pd.DataFrame(analytics.station_delays(warehouse, limit=20, min_observations=5))
stations["mean_delay_min"] = stations["mean_delay_seconds"] / 60

ax = sns.barplot(stations, y="stop_name", x="mean_delay_min", hue="mean_delay_min",
                 palette="rocket_r", legend=False)
ax.set(title="Worst stations by mean delay", xlabel="mean delay (minutes)", ylabel="")
plt.tight_layout(); plt.show()
stations[["stop_name", "observations", "mean_delay_min", "punctuality_pct"]].head(10)
"""),
    MD("## 3. Delay propagation\n\nHow a single service accumulates delay along its route. This is the relationship the ML model learns."),
    CODE("""
worst = pd.DataFrame(analytics.worst_trips(warehouse, limit=5))
worst
"""),
    CODE("""
fig, ax = plt.subplots()
for trip_id in worst["trip_id"].head(4):
    route = pd.DataFrame(analytics.delay_propagation(warehouse, trip_id))
    if route.empty:
        continue
    delay = route["arrival_delay"].fillna(route["departure_delay"]) / 60
    ax.plot(route["stop_sequence"], delay, marker="o", label=trip_id)

ax.axhline(6, ls="--", c="orange", lw=1, label="punctuality threshold")
ax.set(title="Delay accumulation along the route", xlabel="stop sequence", ylabel="delay (minutes)")
ax.legend(fontsize=8); plt.tight_layout(); plt.show()
"""),
    MD("## 4. Route reliability by product\n\nICE, IC, EC and ECE compared on punctuality and mean delay."),
    CODE("""
categories = pd.DataFrame(analytics.category_breakdown(warehouse))
categories["mean_delay_min"] = categories["mean_delay_seconds"] / 60

fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4))
sns.barplot(categories, x="route_category", y="punctuality_pct", hue="route_category",
            legend=False, ax=left).set(title="Punctuality by product", xlabel="", ylabel="%")
sns.barplot(categories, x="route_category", y="mean_delay_min", hue="route_category",
            palette="rocket_r", legend=False, ax=right).set(
    title="Mean delay by product", xlabel="", ylabel="minutes")
plt.tight_layout(); plt.show()
categories
"""),
    MD("## 5. Time series\n\nThe network's delay profile over the collection window — the same series the live dashboard animates."),
    CODE("""
series = pd.DataFrame(analytics.delay_timeseries(warehouse, bucket_minutes=5, hours=24))
series["bucket"] = pd.to_datetime(series["bucket"])

fig, ax = plt.subplots()
ax.plot(series["bucket"], series["mean_delay_seconds"] / 60, lw=2, label="mean delay")
ax.plot(series["bucket"], series["p90_delay_seconds"] / 60, lw=1.2, ls="--", label="p90 delay")
ax.set(title="Network delay over time", xlabel="", ylabel="minutes")

punctuality = ax.twinx()
punctuality.plot(series["bucket"], series["punctuality_pct"], c="seagreen", lw=1.5, label="punctuality")
punctuality.set_ylabel("punctuality (%)"); punctuality.set_ylim(0, 100); punctuality.grid(False)

ax.legend(loc="upper left", fontsize=8); plt.tight_layout(); plt.show()
"""),
    MD("## 6. Disruptions\n\nSkipped station calls, which GTFS-RT reports separately from delay."),
    CODE("""
print(analytics.cancellations(warehouse))
pd.DataFrame(analytics.skipped_stations(warehouse, limit=10))
"""),
    MD("""
## 7. Delay prediction

Given that a train is *N* seconds late at its current stop, how late will it be at the next one?

The benchmark is **persistence** — assume the delay carries over unchanged. On live data 55% of
stop-to-stop deltas are exactly zero, which makes that a strong baseline; the model has to beat it
to be worth anything.
"""),
    CODE("""
from dbrt.ml import DelayModel, load_history, next_stop_pairs

history = load_history(warehouse)
pairs = next_stop_pairs(history)
print(f"{len(history):,} observations -> {len(pairs):,} training pairs")

delta = pairs["next_delay"] - pairs["delay"]
print(f"delta: median {delta.median():.0f}s, {100 * (delta == 0).mean():.1f}% exactly unchanged")
"""),
    CODE("""
model = DelayModel()
metrics = model.train(history)
pd.Series({k: v for k, v in metrics.items() if k != "features"}).to_frame("value")
"""),
    CODE("""
verdict = "beats" if metrics["improvement_pct"] > 0 else "does not beat"
print(f"Model MAE {metrics['mae_seconds']}s {verdict} persistence at {metrics['baseline_mae_seconds']}s "
      f"({metrics['improvement_pct']:+.1f}%)")

fig, ax = plt.subplots(figsize=(6, 3.5))
sns.barplot(x=["persistence baseline", "gradient boosting"],
            y=[metrics["baseline_mae_seconds"], metrics["mae_seconds"]],
            hue=["persistence baseline", "gradient boosting"], legend=False, ax=ax)
ax.set(title="Mean absolute error (lower is better)", ylabel="seconds")
plt.tight_layout(); plt.show()
"""),
]


def main() -> None:
    notebook = nbf.v4.new_notebook(cells=CELLS)
    notebook.metadata = {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": sys.version.split()[0]},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(notebook, OUT)
    print(f"wrote {OUT} ({len(CELLS)} cells)")


if __name__ == "__main__":
    main()
