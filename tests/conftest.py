import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def rt_bytes() -> bytes:
    """Real GTFS-RT payload captured from the live long-distance feed."""
    return (FIXTURES / "gtfs_rt_sample.pb").read_bytes()


@pytest.fixture(scope="session")
def static_zip() -> Path:
    return FIXTURES / "gtfs_static_sample.zip"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES
