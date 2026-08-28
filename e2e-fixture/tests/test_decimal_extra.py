from datetime import timedelta

import pytest

from parser import parse_duration
from service import format_duration, total_minutes


def test_parse_half_hour():
    assert parse_duration('0.5h') == timedelta(minutes=30)


def test_parse_three_quarter_hour():
    assert parse_duration('0.75h') == timedelta(minutes=45)


def test_parse_quarter_minute():
    assert parse_duration('0.25m') == timedelta(seconds=15)


def test_parse_decimal_day_and_hour():
    assert parse_duration('1.5d0.5h') == timedelta(hours=36, minutes=30)


def test_parse_decimal_day_hour_minute():
    assert parse_duration('0.5d12h30m') == timedelta(days=1, minutes=30)


def test_parse_two_and_half_days():
    assert parse_duration('2.5d') == timedelta(hours=60)


def test_parse_iso_lower_rejected():
    with pytest.raises(ValueError) as e:
        parse_duration('p1dt2h')
    assert str(e.value) == 'invalid'


def test_parse_iso_bare_rejected():
    with pytest.raises(ValueError) as e:
        parse_duration('P1D')
    assert str(e.value) == 'invalid'


def test_format_exact_24h_rolls_to_day():
    assert format_duration(timedelta(hours=24)) == '1d'


def test_format_three_quarter_minute():
    assert format_duration(timedelta(seconds=45)) == '0.75m'


def test_format_two_and_quarter_minutes():
    assert format_duration(timedelta(minutes=2, seconds=15)) == '2.25m'


def test_format_tenth_minute():
    assert format_duration(timedelta(seconds=6)) == '0.1m'


def test_format_roundtrip_half_hour():
    assert format_duration(parse_duration('0.5h')) == '30m'


def test_format_roundtrip_half_minute():
    assert format_duration(parse_duration('1.5m')) == '1.5m'
