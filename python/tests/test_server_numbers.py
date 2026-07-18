"""Unit tests for web equipment numeric field parsing.

The equipment forms (instrument/eyepiece) let users type focal length, aperture,
field stop and obstruction. In a comma-decimal browser locale a
``<input type="number">`` yields an empty value for a period-formatted number
(and vice-versa), and a raw comma reaching ``float()`` was silently swallowed by
the handler's broad ``except``. ``parse_number`` accepts either separator and
falls back to a default for missing/blank fields; these tests lock that in.
"""

import pytest

from PiFinder.server import parse_number


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("25.4", 25.4),
        ("25,4", 25.4),
        ("25", 25.0),
        ("  1,5 ", 1.5),
        ("0", 0.0),
    ],
)
def test_parse_number_accepts_both_separators(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("missing", [None, ""])
def test_parse_number_missing_uses_default(missing):
    assert parse_number(missing) == 0.0
    assert parse_number(missing, default="10") == 10.0


@pytest.mark.unit
def test_parse_number_garbage_raises_value_error():
    with pytest.raises(ValueError):
        parse_number("not-a-number")
