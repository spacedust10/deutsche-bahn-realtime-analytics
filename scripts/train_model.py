"""Train the delay model from warehouse history and persist it.

Usage:  PGDATABASE=dbrt python scripts/train_model.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dbrt.config import Settings
from dbrt.ml import DelayModel, load_history
from dbrt.storage import Warehouse

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "delay_model.joblib"


def main() -> None:
    warehouse = Warehouse(Settings.from_env().dsn())
    history = load_history(warehouse)
    print(f"loaded {len(history)} observations")

    model = DelayModel()
    metrics = model.train(history)
    model.save(MODEL_PATH)

    print(json.dumps({k: v for k, v in metrics.items() if k != "features"}, indent=2))
    if metrics["improvement_pct"] <= 0:
        print("\nNOTE: the model does not beat the persistence baseline yet. "
              "Collect more history and retrain.")
    print(f"saved -> {MODEL_PATH}")


if __name__ == "__main__":
    main()
