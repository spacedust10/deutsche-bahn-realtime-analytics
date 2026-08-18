"""Delay prediction.

Question the model answers: a train is N seconds late at its current stop —
how late will it be at the next one?

The benchmark is deliberately unflattering. "Persistence" (assume the delay
carries over unchanged) is already a strong predictor for railways, so a model
that cannot beat it adds nothing. `train()` reports both numbers so the gap is
always visible rather than assumed.

Two choices here were forced by the measured data rather than picked a priori.
On the live feed 55% of stop-to-stop deltas are exactly zero while the delay
itself has a standard deviation near 780s:

  * The target is the *delta* (next_delay - delay), not the absolute next
    delay. Predicting the absolute value made the learner reconstruct a large
    already-known quantity, and it lost to persistence by 56%.
  * The loss is absolute_error, so the model fits the conditional median. With
    a median delta of zero, that lets it say "no change" — which is the
    correct answer most of the time — instead of being dragged off zero by the
    long tail that squared error chases.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split

MIN_TRAINING_SAMPLES = 40

HISTORY_SQL = """
SELECT trip_id, service_date, stop_sequence,
       COALESCE(arrival_delay, departure_delay) AS delay,
       route_category, feed_timestamp
FROM   current_stop_delays
WHERE  COALESCE(arrival_delay, departure_delay) IS NOT NULL
ORDER  BY trip_id, service_date, stop_sequence
"""


def load_history(warehouse) -> pd.DataFrame:
    rows = warehouse.fetchall(HISTORY_SQL)
    return pd.DataFrame(
        rows, columns=["trip_id", "service_date", "stop_sequence", "delay", "route_category", "feed_timestamp"]
    )


def next_stop_pairs(observations: pd.DataFrame) -> pd.DataFrame:
    """Build (current stop, next stop) training pairs.

    Shifting within (trip, service date) groups is what keeps one train's delay
    from being used to predict a different train's — or the same train's on a
    different operating day.
    """
    if observations.empty:
        return observations.assign(next_delay=[])

    df = observations.sort_values(["trip_id", "service_date", "stop_sequence"]).copy()
    grouped = df.groupby(["trip_id", "service_date"], sort=False)
    df["next_delay"] = grouped["delay"].shift(-1)
    df["next_stop_sequence"] = grouped["stop_sequence"].shift(-1)
    df["stops_so_far"] = grouped.cumcount()
    return df.dropna(subset=["next_delay"]).reset_index(drop=True)


def build_features(pairs: pd.DataFrame, columns: Optional[pd.Index] = None) -> tuple[pd.DataFrame, pd.Series]:
    """Feature matrix and target.

    `columns` reindexes to a previously fitted layout so a category that never
    appeared during training cannot change the column set at predict time.
    """
    timestamps = pd.to_datetime(pairs["feed_timestamp"], utc=True, errors="coerce")

    X = pd.DataFrame({
        "delay": pairs["delay"].astype(float),
        "stop_sequence": pairs["stop_sequence"].astype(float),
        "stops_so_far": pairs.get("stops_so_far", pairs["stop_sequence"]).astype(float),
        "hour": timestamps.dt.hour.fillna(12).astype(float),
        "day_of_week": timestamps.dt.dayofweek.fillna(0).astype(float),
    })

    categories = pd.get_dummies(pairs["route_category"].fillna("UNKNOWN"), prefix="cat", dtype=float)
    X = pd.concat([X.reset_index(drop=True), categories.reset_index(drop=True)], axis=1)

    if columns is not None:
        X = X.reindex(columns=columns, fill_value=0.0)

    y = pairs["next_delay"].astype(float) if "next_delay" in pairs else pd.Series(dtype=float)
    return X.fillna(0.0), y


def baseline_mae(pairs: pd.DataFrame) -> float:
    """Error of assuming the current delay simply persists to the next stop."""
    return float(mean_absolute_error(pairs["next_delay"], pairs["delay"]))


class DelayModel:
    def __init__(self):
        self.model: Optional[HistGradientBoostingRegressor] = None
        self.columns: Optional[pd.Index] = None
        self.metrics: dict = {}

    def train(self, observations: pd.DataFrame, test_size: float = 0.25, random_state: int = 42) -> dict:
        pairs = next_stop_pairs(observations)
        if len(pairs) < MIN_TRAINING_SAMPLES:
            raise ValueError(
                f"not enough data to train: {len(pairs)} pairs, need {MIN_TRAINING_SAMPLES}"
            )

        X, y = build_features(pairs)
        self.columns = X.columns
        # Learn the correction, not the absolute delay: see module docstring.
        delta = y - X["delay"]

        X_train, X_test, d_train, d_test = train_test_split(
            X, delta, test_size=test_size, random_state=random_state
        )
        self.model = HistGradientBoostingRegressor(
            loss="absolute_error", max_iter=300, learning_rate=0.08,
            max_depth=6, random_state=random_state,
        )
        self.model.fit(X_train, d_train)

        y_test = d_test + X_test["delay"]
        predicted = X_test["delay"] + self.model.predict(X_test)
        mae = float(mean_absolute_error(y_test, predicted))
        # Baseline is measured on the same held-out rows, otherwise the
        # comparison flatters whichever split happens to be easier.
        base = float(mean_absolute_error(y_test, X_test["delay"]))

        self.metrics = {
            "samples": int(len(pairs)),
            "train_samples": int(len(X_train)),
            "test_samples": int(len(X_test)),
            "mae_seconds": round(mae, 1),
            "baseline_mae_seconds": round(base, 1),
            "improvement_pct": round(100 * (base - mae) / base, 1) if base else 0.0,
            "features": list(self.columns),
        }
        return self.metrics

    def predict(self, pairs: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("model is not trained")
        X, _ = build_features(pairs, columns=self.columns)
        # The model outputs a correction; the caller wants an absolute delay.
        return np.asarray(X["delay"]) + self.model.predict(X)

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": self.model, "columns": self.columns, "metrics": self.metrics}, path)
        return path

    @classmethod
    def load(cls, path: Path | str) -> "DelayModel":
        payload = joblib.load(Path(path))
        instance = cls()
        instance.model = payload["model"]
        instance.columns = payload["columns"]
        instance.metrics = payload.get("metrics", {})
        return instance
