"""Enterprise business rules, tested without a database, a server, or a feed.

These are the rules that would be true about German long-distance rail whether
or not this software existed, so nothing here may need infrastructure to run.
"""
import inspect

import pytest

from dbrt import domain


# --- purity ----------------------------------------------------------------

def test_the_domain_imports_nothing_outside_the_standard_library():
    """The innermost circle cannot mention an outer one. If this fails, a
    framework, driver or SQL dialect has leaked into the business rules."""
    source = inspect.getsource(domain)
    forbidden = ("psycopg2", "fastapi", "requests", "sklearn", "pandas",
                 "google.transit", "SELECT", "INSERT", "warehouse")
    for name in forbidden:
        assert name not in source, f"{name} leaked into the domain layer"


# --- punctuality -----------------------------------------------------------

def test_punctuality_uses_db_own_six_minute_definition():
    assert domain.PUNCTUALITY_THRESHOLD_SECONDS == 360


@pytest.mark.parametrize("delay,punctual", [
    (-600, True), (0, True), (359, True), (360, False), (361, False), (7200, False),
])
def test_is_punctual_is_strictly_under_the_threshold(delay, punctual):
    assert domain.is_punctual(delay) is punctual


def test_a_stop_with_no_prediction_is_not_punctual_and_not_late():
    """Missing is not zero. Counting it either way biases every number."""
    assert domain.is_punctual(None) is None


# --- delay bands -----------------------------------------------------------

def test_bands_are_contiguous_with_no_gaps_or_overlaps():
    """The defect this layer exists to prevent: three band tables in two
    languages that quietly disagreed about where 'on time' ends."""
    bands = domain.DELAY_BANDS
    assert bands[0].lower is None, "the first band must be open at the bottom"
    assert bands[-1].upper is None, "the last band must be open at the top"
    for previous, following in zip(bands, bands[1:]):
        assert previous.upper == following.lower, f"gap between {previous.key} and {following.key}"


def test_every_band_key_is_unique_and_stable():
    keys = [b.key for b in domain.DELAY_BANDS]
    assert len(keys) == len(set(keys))
    assert all(k.replace("_", "").isalnum() for k in keys)


def test_the_on_time_band_ends_exactly_at_the_punctuality_threshold():
    """The map legend and the histogram must not disagree about 'on time'."""
    on_time = domain.band_for(0)
    assert on_time.upper == domain.PUNCTUALITY_THRESHOLD_SECONDS


@pytest.mark.parametrize("delay,key", [
    (-300, "early"), (-1, "early"), (0, "on_time"), (359, "on_time"),
    (360, "late_6_15"), (899, "late_6_15"), (900, "late_15_30"),
    (1800, "late_30_60"), (3600, "late_60_plus"), (99999, "late_60_plus"),
])
def test_band_for_classifies_on_the_documented_boundaries(delay, key):
    assert domain.band_for(delay).key == key


def test_band_for_covers_every_integer_it_could_ever_receive():
    for delay in range(-4000, 8000, 7):
        assert domain.band_for(delay) is not None


def test_band_for_none_is_none_rather_than_a_guess():
    assert domain.band_for(None) is None


def test_severity_never_decreases_as_delay_grows():
    """Severity drives the colour ramp, so it has to be monotonic or the map
    shows a worse delay in a calmer colour."""
    severities = [b.severity for b in domain.DELAY_BANDS if b.key != "early"]
    assert severities == sorted(severities)


def test_early_shares_the_calmest_severity_with_on_time():
    assert domain.band_for(-600).severity == domain.band_for(0).severity


def test_severity_levels_fit_the_four_step_colour_ramp():
    assert {b.severity for b in domain.DELAY_BANDS} == {0, 1, 2, 3}


# --- train categories ------------------------------------------------------

def test_long_distance_scope_matches_the_project_brief():
    assert {"ICE", "IC", "EC"} <= domain.LONG_DISTANCE_CATEGORIES


@pytest.mark.parametrize("short_name,category", [
    ("ICE 41", "ICE"), ("IC 35", "IC"), ("EC 62", "EC"), ("ECE", "ECE"),
    ("  ICE  11 ", "ICE"), ("", ""), (None, ""),
])
def test_category_of_takes_the_leading_token(short_name, category):
    assert domain.category_of(short_name) == category


def test_is_long_distance_accepts_only_the_scoped_products():
    assert domain.is_long_distance("ICE") is True
    assert domain.is_long_distance("RE") is False
    assert domain.is_long_distance(None) is False
