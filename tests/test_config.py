"""Settings resolution, especially which realtime source gets used."""
import pytest

from dbrt.config import Settings


def test_defaults_point_at_official_db_fernverkehr_endpoints():
    s = Settings.from_env({})
    assert s.db_gtfs_rt_url.startswith("https://gtfs-datenstroeme.tech.deutschebahn.com/db-fernverkehr/")
    assert s.db_gtfs_rt_url.endswith("gtfsrt_trip_updates.proto")
    assert s.db_gtfs_static_url.endswith("gtfs.zip")


def test_official_source_selected_when_api_key_present():
    s = Settings.from_env({"DB_API_KEY": "secret-key"})
    src = s.realtime_source()
    assert src.official is True
    assert src.url == s.db_gtfs_rt_url
    assert src.headers == {"DB-Api-Key": "secret-key"}


def test_open_fallback_selected_when_api_key_absent():
    s = Settings.from_env({})
    src = s.realtime_source()
    assert src.official is False
    assert src.url == s.fallback_gtfs_rt_url
    assert src.headers == {}


def test_blank_api_key_is_treated_as_absent():
    s = Settings.from_env({"DB_API_KEY": "   "})
    assert s.realtime_source().official is False


def test_static_source_follows_the_same_key_rule():
    assert Settings.from_env({"DB_API_KEY": "k"}).static_source().official is True
    assert Settings.from_env({}).static_source().official is False


def test_poll_interval_is_read_as_int_and_floored_at_ten_seconds():
    assert Settings.from_env({"POLL_INTERVAL_SECONDS": "20"}).poll_interval_seconds == 20
    # Guard against hammering the upstream feed with a misconfigured value.
    assert Settings.from_env({"POLL_INTERVAL_SECONDS": "1"}).poll_interval_seconds == 10


def test_invalid_poll_interval_falls_back_to_default_rather_than_crashing():
    assert Settings.from_env({"POLL_INTERVAL_SECONDS": "abc"}).poll_interval_seconds == 60


def test_dsn_is_assembled_from_pg_environment():
    s = Settings.from_env({"PGHOST": "db.internal", "PGPORT": "6543", "PGDATABASE": "warehouse", "PGUSER": "rail"})
    assert "host=db.internal" in s.dsn()
    assert "port=6543" in s.dsn()
    assert "dbname=warehouse" in s.dsn()
    assert "user=rail" in s.dsn()


def test_dsn_omits_empty_credentials_so_local_peer_auth_works():
    s = Settings.from_env({"PGUSER": "", "PGPASSWORD": ""})
    assert "user=" not in s.dsn()
    assert "password=" not in s.dsn()
