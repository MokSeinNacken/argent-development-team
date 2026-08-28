from datetime import timedelta

import pytest

from parser import parse_duration
from service import format_duration, total_minutes


def test_decimal_day_precision_micros():
    assert parse_duration('0.3d') == timedelta(hours=7, minutes=12)


def test_decimal_hour_precision_micros():
    assert parse_duration('1.5h') == timedelta(microseconds=5_400_000_000)


def test_decimal_minute_precision_micros():
    assert parse_duration('1.25m') == timedelta(microseconds=75_000_000)


def test_mixed_decimal_exact_24h():
    assert parse_duration('0.5d12h') == timedelta(hours=24)
    assert parse_duration('0.5d12h') == timedelta(days=1)


def test_mixed_decimal_hour_minute_trap():
    assert parse_duration('1.5h30m') == timedelta(hours=2)


def test_sum_components_no_float_artifact():
    assert parse_duration('1.5h1.5m') == timedelta(hours=1, minutes=31, seconds=30)


def test_decimal_negative_rejected():
    for s in ('-1.5d', '-0.5h', '-1.25m'):
        with pytest.raises(ValueError) as e:
            parse_duration(s)
        assert str(e.value) == 'negative'


def test_iso_forms_rejected():
    for s in ('P1DT2H', 'PT2H', 'P1D', 'p1dt2h'):
        with pytest.raises(ValueError) as e:
            parse_duration(s)
        assert str(e.value) == 'invalid'


def test_format_decimal_outputs():
    assert format_duration(timedelta(seconds=6)) == '0.1m'
    assert format_duration(timedelta(seconds=15)) == '0.25m'
    assert format_duration(timedelta(seconds=30)) == '0.5m'
    assert format_duration(timedelta(seconds=45)) == '0.75m'
    assert format_duration(timedelta(minutes=1, seconds=30)) == '1.5m'
    assert format_duration(timedelta(minutes=2, seconds=15)) == '2.25m'


def test_format_roundtrip_stable():
    assert format_duration(parse_duration('0.5d')) == '12h'
    assert format_duration(parse_duration('1.5h')) == '1h30m'
    assert format_duration(parse_duration('1.25m')) == '1.25m'
