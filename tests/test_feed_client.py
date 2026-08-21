"""HTTPS retrieval of the realtime feed, including conditional requests."""
import pytest

from dbrt.config import Settings
from dbrt.feed_client import FeedClient, FeedResult


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Records outgoing requests so header and caching behaviour is assertable."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        return self.responses.pop(0)


def test_fetch_returns_payload_and_records_the_etag():
    session = FakeSession(FakeResponse(200, b"\x00binary", {"ETag": "abc123"}))
    client = FeedClient(Settings.from_env({}), session=session)

    result = client.fetch()

    assert isinstance(result, FeedResult)
    assert result.payload == b"\x00binary"
    assert result.status == 200
    assert result.not_modified is False
    assert client.etag == "abc123"


def test_second_fetch_sends_if_none_match_and_reports_not_modified():
    session = FakeSession(
        FakeResponse(200, b"payload", {"ETag": "abc123"}),
        FakeResponse(304, b"", {}),
    )
    client = FeedClient(Settings.from_env({}), session=session)

    client.fetch()
    second = client.fetch()

    assert session.calls[1]["headers"]["If-None-Match"] == "abc123"
    assert second.not_modified is True
    assert second.payload is None


def test_api_key_is_sent_as_the_db_api_key_header():
    session = FakeSession(FakeResponse(200, b"x", {}))
    client = FeedClient(Settings.from_env({"DB_API_KEY": "topsecret"}), session=session)

    client.fetch()

    assert session.calls[0]["headers"]["DB-Api-Key"] == "topsecret"
    assert "gtfs-datenstroeme.tech.deutschebahn.com" in session.calls[0]["url"]


def test_without_a_key_the_open_fallback_endpoint_is_used_and_no_key_header_sent():
    session = FakeSession(FakeResponse(200, b"x", {}))
    client = FeedClient(Settings.from_env({}), session=session)

    client.fetch()

    assert session.calls[0]["url"] == "https://realtime.gtfs.de/realtime-free.pb"
    assert "DB-Api-Key" not in session.calls[0]["headers"]


def test_a_request_timeout_is_always_set():
    session = FakeSession(FakeResponse(200, b"x", {}))
    FeedClient(Settings.from_env({}), session=session).fetch()
    assert session.calls[0]["timeout"] is not None


def test_http_error_propagates_so_the_collector_can_log_and_retry():
    session = FakeSession(FakeResponse(503, b"", {}))
    with pytest.raises(RuntimeError, match="503"):
        FeedClient(Settings.from_env({}), session=session).fetch()


def test_etag_is_not_overwritten_by_a_304_response():
    session = FakeSession(
        FakeResponse(200, b"p", {"ETag": "keep-me"}),
        FakeResponse(304, b"", {}),
    )
    client = FeedClient(Settings.from_env({}), session=session)
    client.fetch()
    client.fetch()
    assert client.etag == "keep-me"


def test_result_reports_the_source_label_for_data_lineage():
    session = FakeSession(FakeResponse(200, b"x", {}))
    result = FeedClient(Settings.from_env({"DB_API_KEY": "k"}), session=session).fetch()
    assert result.source_label == "DB Fernverkehr (official)"
    assert result.official is True


# --- static timetable download ---------------------------------------------

class _Resp:
    def __init__(self, content=b"zipbytes", status_code=200):
        self.content, self.status_code = content, status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return _Resp()

def test_download_static_writes_the_zip_and_returns_its_path(tmp_path):
    from dbrt.config import Settings
    from dbrt.feed_client import download_static

    session = _Session()
    path = download_static(Settings.from_env({}), tmp_path / "gtfs.zip", session=session)

    assert path.exists()
    assert path.read_bytes() == b"zipbytes"
    assert session.calls == ["https://download.gtfs.de/germany/fv_free/latest.zip"]


def test_download_static_skips_the_network_when_a_fresh_copy_exists(tmp_path):
    from dbrt.config import Settings
    from dbrt.feed_client import download_static

    dest = tmp_path / "gtfs.zip"
    dest.write_bytes(b"cached")
    session = _Session()

    path = download_static(Settings.from_env({}), dest, session=session, max_age_seconds=3600)

    assert path.read_bytes() == b"cached"
    assert session.calls == [], "a fresh cached timetable must not be re-downloaded"


def test_download_static_refreshes_when_the_cached_copy_is_stale(tmp_path):
    from dbrt.config import Settings
    from dbrt.feed_client import download_static

    dest = tmp_path / "gtfs.zip"
    dest.write_bytes(b"stale")
    session = _Session()

    download_static(Settings.from_env({}), dest, session=session, max_age_seconds=0)

    assert session.calls, "an expired timetable must be refetched"
    assert dest.read_bytes() == b"zipbytes"


def test_download_static_uses_the_official_endpoint_when_a_key_is_configured(tmp_path):
    from dbrt.config import Settings
    from dbrt.feed_client import download_static

    session = _Session()
    download_static(Settings.from_env({"DB_API_KEY": "k"}), tmp_path / "g.zip", session=session)
    assert session.calls[0].endswith("db-fernverkehr/gtfs.zip")
