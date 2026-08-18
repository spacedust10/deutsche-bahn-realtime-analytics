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

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import KFold

MIN_TRAINING_SAMPLES = 40
CV_FOLDS = 5

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


def build_features(pairs: pd.DataFrame, columns: pd.Index | None = None) -> tuple[pd.DataFrame, pd.Series]:
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
        self.model: HistGradientBoostingRegressor | None = None
        self.columns: pd.Index | None = None
        self.metrics: dict = {}

    def _estimator(self, random_state: int) -> HistGradientBoostingRegressor:
        return HistGradientBoostingRegressor(
            loss="absolute_error", max_iter=300, learning_rate=0.08,
            max_depth=6, random_state=random_state,
        )

    def train(self, observations: pd.DataFrame, folds: int = CV_FOLDS, random_state: int = 42) -> dict:
        """Score by cross-validation, then fit the shipped model on everything.

        A single hold-out split on a few thousand pairs moved the reported
        improvement by several points between runs — noise presented as signal.
        Averaging folds gives a number worth quoting, and the standard
        deviation across folds says how much to trust it.
        """
        pairs = next_stop_pairs(observations)
        if len(pairs) < MIN_TRAINING_SAMPLES:
            raise ValueError(
                f"not enough data to train: {len(pairs)} pairs, need {MIN_TRAINING_SAMPLES}"
            )

        X, y = build_features(pairs)
        self.columns = X.columns
        # Learn the correction, not the absolute delay: see module docstring.
        delta = y - X["delay"]

        folds = max(2, min(folds, len(pairs) // 10))
        splitter = KFold(n_splits=folds, shuffle=True, random_state=random_state)

        fold_mae, fold_base, fold_rmse, fold_base_rmse = [], [], [], []
        for train_idx, test_idx in splitter.split(X):
            X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
            d_train, d_test = delta.iloc[train_idx], delta.iloc[test_idx]

            estimator = self._estimator(random_state)
            estimator.fit(X_train, d_train)

            y_test = d_test + X_test["delay"]
            predicted = X_test["delay"] + estimator.predict(X_test)
            fold_mae.append(float(mean_absolute_error(y_test, predicted)))
            # Baseline scored on the identical fold, otherwise the comparison
            # flatters whichever split happens to be easier.
            fold_base.append(float(mean_absolute_error(y_test, X_test["delay"])))
            # RMSE is reported too: MAE rewards the median, RMSE the mean, and
            # quoting only one hides which behaviour the model was tuned for.
            fold_rmse.append(float(mean_squared_error(y_test, predicted)) ** 0.5)
            fold_base_rmse.append(float(mean_squared_error(y_test, X_test["delay"])) ** 0.5)

        mae = float(np.mean(fold_mae))
        base = float(np.mean(fold_base))

        # The measurement is done; the shipped model gets all the data.
        self.model = self._estimator(random_state)
        self.model.fit(X, delta)

        self.metrics = {
            "samples": int(len(pairs)),
            "cv_folds": folds,
            "mae_seconds": round(mae, 1),
            "mae_std_seconds": round(float(np.std(fold_mae)), 1),
            "baseline_mae_seconds": round(base, 1),
            "rmse_seconds": round(float(np.mean(fold_rmse)), 1),
            "baseline_rmse_seconds": round(float(np.mean(fold_base_rmse)), 1),
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
    def load(cls, path: Path | str) -> DelayModel:
        payload = joblib.load(Path(path))
        instance = cls()
        instance.model = payload["model"]
        instance.columns = payload["columns"]
        instance.metrics = payload.get("metrics", {})
        return instance
