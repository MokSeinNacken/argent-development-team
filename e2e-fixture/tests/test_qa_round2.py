from datetime import timedelta

import pytest

from parser import parse_duration
from service import format_duration


def test_parse_decimal_hour_trap():
    assert parse_duration('1.5h30m') == timedelta(hours=2)


def test_parse_decimal_day_hour_exact_24h():
    assert parse_duration('0.5d12h') == timedelta(days=1)
    assert parse_duration('0.5d12h') == timedelta(hours=24)


def test_parse_decimal_hour_exact_micros():
    assert parse_duration('1.5h') == timedelta(microseconds=5400000000)


def test_parse_decimal_day_exact_micros():
    assert parse_duration('0.5d') == timedelta(microseconds=43200000000)


def test_parse_decimal_minute_exact_micros():
    assert parse_duration('1.25m') == timedelta(microseconds=75000000)


def test_parse_sum_components_no_float_artifact():
    assert parse_duration('1.5h1.5m') == timedelta(hours=1, minutes=31, seconds=30)


def test_parse_iso_forms_rejected():
    for bad in ('P1DT2H', 'P1D', 'PT2H', 'p1dt2h'):
        with pytest.raises(ValueError) as exc:
            parse_duration(bad)
        assert str(exc.value) == 'invalid'


def test_parse_negative_decimal_rejected():
    for bad in ('-1.5d', '-0.5h', '-1.25m'):
        with pytest.raises(ValueError) as exc:
            parse_duration(bad)
        assert str(exc.value) == 'negative'


def test_format_decimal_outputs():
    assert format_duration(timedelta(seconds=30)) == '0.5m'
    assert format_duration(timedelta(seconds=15)) == '0.25m'
    assert format_duration(timedelta(seconds=45)) == '0.75m'
    assert format_duration(timedelta(seconds=6)) == '0.1m'
    assert format_duration(timedelta(minutes=1, seconds=30)) == '1.5m'
    assert format_duration(timedelta(minutes=2, seconds=15)) == '2.25m'


def test_format_roundtrip_stable():
    assert format_duration(parse_duration('0.5h')) == '30m'
    assert format_duration(parse_duration('1.5m')) == '1.5m'
    assert format_duration(parse_duration('1.25m')) == '1.25m'
    assert format_duration(parse_duration('1.5h30m')) == '2h'
    assert format_duration(parse_duration('0.5d12h')) == '1d'
