"""Delay prediction.

The model answers a concrete operational question: given that a train is N
seconds late at its current stop, how late will it be at the next one? The
honest bar is the persistence baseline (assume the delay simply carries over),
so the tests hold the model to beating it rather than to an absolute error.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from dbrt.ml import (
    DelayModel,
    baseline_mae,
    build_features,
    next_stop_pairs,
)


def frame(rows):
    return pd.DataFrame(
        rows,
        columns=["trip_id", "service_date", "stop_sequence", "delay", "route_category", "feed_timestamp"],
    )


@pytest.fixture()
def observations():
    base = dt.datetime(2026, 8, 18, 7, 0, tzinfo=dt.timezone.utc)
    rows = []
    for trip in range(60):
        delay = (trip % 7) * 60
        for seq in range(6):
            # Delay grows along the route, faster for already-late trains.
            delay = delay + 30 + (delay // 10)
            rows.append([f"T{trip}", dt.date(2026, 8, 18), seq, delay, "ICE" if trip % 3 else "IC",
                         base + dt.timedelta(minutes=seq)])
    return frame(rows)


# --- pair construction -----------------------------------------------------

def test_next_stop_pairs_links_each_stop_to_the_following_one(observations):
    pairs = next_stop_pairs(observations)
    assert not pairs.empty
    assert {"delay", "next_delay", "stop_sequence"} <= set(pairs.columns)


def test_pairs_never_cross_trip_boundaries():
    rows = [
        ["A", dt.date(2026, 8, 18), 0, 60, "ICE", dt.datetime(2026, 8, 18, 7, tzinfo=dt.timezone.utc)],
        ["A", dt.date(2026, 8, 18), 1, 120, "ICE", dt.datetime(2026, 8, 18, 7, tzinfo=dt.timezone.utc)],
        ["B", dt.date(2026, 8, 18), 0, 900, "ICE", dt.datetime(2026, 8, 18, 7, tzinfo=dt.timezone.utc)],
    ]
    pairs = next_stop_pairs(frame(rows))
    # Only A(0->1) is a legitimate pair; B's single stop has no successor.
    assert len(pairs) == 1
    assert pairs.iloc[0]["delay"] == 60
    assert pairs.iloc[0]["next_delay"] == 120


def test_pairs_never_cross_service_dates():
    rows = [
        ["A", dt.date(2026, 8, 18), 0, 60, "ICE", dt.datetime(2026, 8, 18, 7, tzinfo=dt.timezone.utc)],
        ["A", dt.date(2026, 8, 19), 1, 999, "ICE", dt.datetime(2026, 8, 19, 7, tzinfo=dt.timezone.utc)],
    ]
    assert next_stop_pairs(frame(rows)).empty


def test_empty_history_yields_no_pairs():
    assert next_stop_pairs(frame([])).empty


# --- features --------------------------------------------------------------

def test_features_include_delay_sequence_hour_and_category(observations):
    X, y = build_features(next_stop_pairs(observations))
    assert {"delay", "stop_sequence", "hour", "day_of_week"} <= set(X.columns)
    assert any(c.startswith("cat_") for c in X.columns), "category must be encoded"
    assert len(X) == len(y)


def test_features_contain_no_nulls(observations):
    X, _ = build_features(next_stop_pairs(observations))
    assert not X.isnull().any().any()


def test_category_encoding_is_stable_between_train_and_predict(observations):
    """A category unseen at predict time must not shift the column layout."""
    pairs = next_stop_pairs(observations)
    X_train, _ = build_features(pairs)
    unseen = pairs.head(3).copy()
    unseen["route_category"] = "EC"
    X_pred, _ = build_features(unseen, columns=X_train.columns)
    assert list(X_pred.columns) == list(X_train.columns)


# --- model -----------------------------------------------------------------

def test_model_trains_and_reports_metrics(observations):
    model = DelayModel()
    metrics = model.train(observations)
    assert metrics["samples"] > 0
    assert metrics["mae_seconds"] >= 0
    assert "baseline_mae_seconds" in metrics


def test_model_beats_the_persistence_baseline(observations):
    """If it cannot beat 'the delay stays the same', it is not worth shipping."""
    metrics = DelayModel().train(observations)
    assert metrics["mae_seconds"] < metrics["baseline_mae_seconds"]
    assert metrics["improvement_pct"] > 0


def test_baseline_mae_is_the_error_of_assuming_no_change():
    pairs = pd.DataFrame({"delay": [0, 100, 200], "next_delay": [60, 100, 260]})
    assert baseline_mae(pairs) == pytest.approx(40.0)


def test_predict_returns_one_number_per_input_row(observations):
    model = DelayModel()
    model.train(observations)
    preds = model.predict(next_stop_pairs(observations).head(5))
    assert len(preds) == 5
    assert all(np.isfinite(preds))


def test_predicting_before_training_raises_a_clear_error(observations):
    with pytest.raises(RuntimeError, match="not trained"):
        DelayModel().predict(next_stop_pairs(observations).head(1))


def test_training_on_too_little_data_raises_rather_than_fitting_noise():
    tiny = frame([["A", dt.date(2026, 8, 18), 0, 60, "ICE", dt.datetime(2026, 8, 18, 7, tzinfo=dt.timezone.utc)]])
    with pytest.raises(ValueError, match="not enough"):
        DelayModel().train(tiny)


def test_model_round_trips_through_disk(observations, tmp_path):
    model = DelayModel()
    model.train(observations)
    expected = model.predict(next_stop_pairs(observations).head(3))

    path = model.save(tmp_path / "delay.joblib")
    restored = DelayModel.load(path)

    assert np.allclose(restored.predict(next_stop_pairs(observations).head(3)), expected)


def test_a_later_train_is_predicted_later_than_a_punctual_one(observations):
    """Sanity: the model must learn the direction of the relationship."""
    model = DelayModel()
    model.train(observations)
    pairs = next_stop_pairs(observations)
    punctual = pairs.head(1).copy(); punctual["delay"] = 0
    very_late = pairs.head(1).copy(); very_late["delay"] = 1800
    assert model.predict(very_late)[0] > model.predict(punctual)[0]
