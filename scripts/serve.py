"""Run the dashboard:  PGDATABASE=dbrt python scripts/serve.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from dbrt.api import create_app
from dbrt.config import Settings

if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port, log_level="info")
