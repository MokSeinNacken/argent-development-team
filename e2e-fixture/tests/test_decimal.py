from datetime import timedelta

import pytest

from parser import parse_duration
from service import format_duration, total_minutes


def test_decimal_hours():
    assert parse_duration('1.5h') == timedelta(hours=1, minutes=30)


def test_decimal_days():
    assert parse_duration('0.5d') == timedelta(hours=12)


def test_decimal_minutes():
    assert parse_duration('1.25m') == timedelta(minutes=1, seconds=15)


def test_decimal_mixed_hours_minutes():
    assert parse_duration('1.5h30m') == timedelta(hours=2)


def test_decimal_days_hours_exact():
    assert parse_duration('0.5d12h') == timedelta(days=1)


def test_decimal_days_hours_exact_hours():
    assert parse_duration('0.5d12h') == timedelta(hours=24)


def test_decimal_partial_day_precision():
    assert parse_duration('0.1d') == timedelta(hours=2, minutes=24)


def test_iso_style_rejected():
    with pytest.raises(ValueError) as e:
        parse_duration('P1DT2H')
    assert str(e.value) == 'invalid'


def test_decimal_negative_rejected():
    with pytest.raises(ValueError) as e:
        parse_duration('-1.5d')
    assert str(e.value) == 'negative'


def test_format_decimal_half_minute():
    assert format_duration(timedelta(minutes=1, seconds=30)) == '1.5m'


def test_format_decimal_zero_minutes_half():
    assert format_duration(timedelta(seconds=30)) == '0.5m'


def test_format_decimal_quarter_minute():
    assert format_duration(timedelta(seconds=15)) == '0.25m'


def test_format_roundtrip_decimal_minutes():
    assert format_duration(parse_duration('1.25m')) == '1.25m'


def test_format_roundtrip_decimal_hours():
    assert format_duration(parse_duration('1.5h30m')) == '2h'


def test_format_roundtrip_decimal_days():
    assert format_duration(parse_duration('0.5d12h')) == '1d'
