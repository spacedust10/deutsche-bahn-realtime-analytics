"""Settings resolution, especially which realtime source gets used."""
import pytest

from dbrt.config import DEFAULT_POLL_SECONDS, Settings


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
    assert Settings.from_env({"POLL_INTERVAL_SECONDS": "abc"}).poll_interval_seconds == DEFAULT_POLL_SECONDS


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


# --- .env loading ----------------------------------------------------------

def test_load_dotenv_reads_key_value_pairs(tmp_path):
    from dbrt.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("DB_API_KEY=abc123\nPGDATABASE=warehouse\n")

    assert load_dotenv(env_file) == {"DB_API_KEY": "abc123", "PGDATABASE": "warehouse"}


def test_load_dotenv_ignores_comments_and_blank_lines(tmp_path):
    from dbrt.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("# a comment\n\nPGHOST=db\n   \n#PGPORT=9999\n")

    assert load_dotenv(env_file) == {"PGHOST": "db"}


def test_load_dotenv_strips_surrounding_quotes(tmp_path):
    from dbrt.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text('DB_API_KEY="quoted key"\nPGUSER=\'single\'\n')

    assert load_dotenv(env_file) == {"DB_API_KEY": "quoted key", "PGUSER": "single"}


def test_load_dotenv_keeps_equals_signs_inside_values(tmp_path):
    from dbrt.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("PGPASSWORD=a=b=c\n")

    assert load_dotenv(env_file)["PGPASSWORD"] == "a=b=c"


def test_load_dotenv_on_a_missing_file_is_empty_not_an_error(tmp_path):
    from dbrt.config import load_dotenv

    assert load_dotenv(tmp_path / "nope.env") == {}


def test_real_environment_wins_over_the_dotenv_file(tmp_path, monkeypatch):
    """A deliberate export must not be silently overridden by a stale .env."""
    from dbrt.config import DEFAULT_POLL_SECONDS, Settings

    env_file = tmp_path / ".env"
    env_file.write_text("PGDATABASE=from_file\n")
    monkeypatch.setenv("PGDATABASE", "from_shell")

    assert Settings.from_env(dotenv_path=env_file).pgdatabase == "from_shell"


def test_dotenv_values_are_used_when_the_environment_is_silent(tmp_path, monkeypatch):
    from dbrt.config import DEFAULT_POLL_SECONDS, Settings

    env_file = tmp_path / ".env"
    env_file.write_text("PGDATABASE=from_file\nDB_API_KEY=filekey\n")
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("DB_API_KEY", raising=False)

    settings = Settings.from_env(dotenv_path=env_file)
    assert settings.pgdatabase == "from_file"
    assert settings.realtime_source().official is True
