"""Enterprise business rules for German long-distance rail punctuality.

This is the innermost circle. Everything here would still be true if the
project used a different database, served a different UI, or read the feed from
a file: a train is late by some number of seconds, Deutsche Bahn calls anything
under six minutes punctual, and the long-distance products are ICE, IC and EC.

Nothing in this module may import a driver, a framework, or an outer module, and
nothing here may contain SQL. `tests/test_domain.py` enforces that by reading
this file's own source, because the rule is easy to state and easy to erode.

It exists because these rules had been scattered: the punctuality threshold was
a named constant in one place and a bare `360` inside a SQL string in another,
while the delay bands were written out three times across Python and JavaScript
with boundaries that had drifted apart. The dashboard showed two different
definitions of "on time" at once. Every consumer now derives from here.
"""
from __future__ import annotations

from dataclasses import dataclass

# Deutsche Bahn's own published definition of "pünktlich". Using their threshold
# rather than inventing one keeps every number here comparable to DB's figures.
PUNCTUALITY_THRESHOLD_SECONDS = 360

# The products the project is scoped to, plus the EuroCity Express variant DB
# publishes alongside EC.
LONG_DISTANCE_CATEGORIES = frozenset({"ICE", "IC", "EC", "ECE"})


@dataclass(frozen=True)
class DelayBand:
    """One bucket of the delay scale.

    Bounds are seconds, `lower` inclusive and `upper` exclusive, with `None`
    meaning open. `severity` is an ordinal that the presentation layer maps to a
    colour step, so no consumer needs to know the numeric boundaries to draw the
    right shade.
    """

    key: str
    label: str
    lower: int | None
    upper: int | None
    severity: int

    def contains(self, delay_seconds: int) -> bool:
        if self.lower is not None and delay_seconds < self.lower:
            return False
        return self.upper is None or delay_seconds < self.upper


# Contiguous and exhaustive by construction; the tests assert both, since a gap
# here is a delay that silently belongs to no band.
#
# The on-time band ends exactly at PUNCTUALITY_THRESHOLD_SECONDS. That is the
# single fact the map legend, the histogram and the headline metric had
# previously disagreed about.
DELAY_BANDS: tuple[DelayBand, ...] = (
    DelayBand("early",        "Early",            None,                          0,    severity=0),
    DelayBand("on_time",      "On time (<6 min)", 0,     PUNCTUALITY_THRESHOLD_SECONDS, severity=0),
    DelayBand("late_6_15",    "6-15 min",         PUNCTUALITY_THRESHOLD_SECONDS, 900,  severity=1),
    DelayBand("late_15_30",   "15-30 min",        900,                           1800, severity=2),
    DelayBand("late_30_60",   "30-60 min",        1800,                          3600, severity=3),
    DelayBand("late_60_plus", "60+ min",          3600,                          None, severity=3),
)


def is_punctual(delay_seconds: int | None) -> bool | None:
    """True when a call met DB's threshold.

    None in, None out: a stop the feed made no prediction for is neither
    punctual nor late, and counting it either way biases every rate downstream.
    """
    if delay_seconds is None:
        return None
    return delay_seconds < PUNCTUALITY_THRESHOLD_SECONDS


def band_for(delay_seconds: int | None) -> DelayBand | None:
    """The band a delay falls into, or None when there is no prediction."""
    if delay_seconds is None:
        return None
    for band in DELAY_BANDS:
        if band.contains(delay_seconds):
            return band
    return DELAY_BANDS[-1]   # Unreachable while the bands stay exhaustive.


def severity_of(delay_seconds: int | None) -> int | None:
    band = band_for(delay_seconds)
    return band.severity if band else None


def category_of(route_short_name: str | None) -> str:
    """"ICE 41" -> "ICE". The feed puts the line number in route_short_name."""
    if not route_short_name:
        return ""
    stripped = route_short_name.strip()
    return stripped.split()[0] if stripped else ""


def is_long_distance(category: str | None) -> bool:
    return category in LONG_DISTANCE_CATEGORIES
