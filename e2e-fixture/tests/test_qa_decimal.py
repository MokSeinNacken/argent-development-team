from datetime import timedelta

import pytest

from parser import parse_duration
from service import format_duration


def test_parse_three_tenths_day():
    assert parse_duration('0.3d') == timedelta(hours=7, minutes=12)


def test_parse_one_and_half_days_plus_hours():
    assert parse_duration('1.5d12h') == timedelta(days=2)


def test_parse_decimal_hours_and_minutes_mixed():
    assert parse_duration('1.5h1.5m') == timedelta(hours=1, minutes=31, seconds=30)


def test_format_partial_day_roundtrip():
    assert format_duration(parse_duration('0.1d')) == '2h24m'


def test_parse_negative_fractional_hour():
    with pytest.raises(ValueError) as e:
        parse_duration('1d-0.5h')
    assert str(e.value) == 'negative'


def test_parse_iso_time_only_rejected():
    with pytest.raises(ValueError) as e:
        parse_duration('PT2H')
    assert str(e.value) == 'invalid'
