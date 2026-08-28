from datetime import timedelta

import pytest

from parser import parse_duration
from service import format_duration


def test_decimal_hours_exact_microseconds():
    assert parse_duration('1.5h') == timedelta(microseconds=5400000000)


def test_decimal_days_exact_microseconds():
    assert parse_duration('0.5d') == timedelta(microseconds=43200000000)


def test_decimal_minutes_exact_microseconds():
    assert parse_duration('1.25m') == timedelta(microseconds=75000000)


def test_mixed_hours_minutes_trap():
    got = parse_duration('1.5h30m')
    assert got == timedelta(hours=2)
    assert got.total_seconds() == 7200.0


def test_days_hours_exact_24h_trap():
    got = parse_duration('0.5d12h')
    assert got == timedelta(days=1)
    assert got == timedelta(hours=24)
    assert got.total_seconds() == 86400.0


def test_sum_components_exact_no_float_artifacts():
    got = parse_duration('1.5h1.5m')
    assert got == timedelta(hours=1, minutes=31, seconds=30)
    assert got.total_seconds() == 5490.0


def test_iso_forms_rejected():
    for bad in ('P1DT2H', 'p1dt2h', 'P1D', 'PT2H'):
        with pytest.raises(ValueError) as e:
            parse_duration(bad)
        assert str(e.value) == 'invalid'


def test_negative_decimal_forms_rejected():
    for bad in ('-1.5d', '1d-0.5h', '-0.5h'):
        with pytest.raises(ValueError) as e:
            parse_duration(bad)
        assert str(e.value) == 'negative'


def test_format_decimal_outputs():
    assert format_duration(timedelta(minutes=1, seconds=30)) == '1.5m'
    assert format_duration(timedelta(seconds=30)) == '0.5m'
    assert format_duration(timedelta(seconds=15)) == '0.25m'
    assert format_duration(timedelta(seconds=45)) == '0.75m'
    assert format_duration(timedelta(seconds=6)) == '0.1m'


def test_format_roundtrip_decimal():
    assert format_duration(parse_duration('1.25m')) == '1.25m'
    assert format_duration(parse_duration('0.5d12h')) == '1d'
    assert format_duration(parse_duration('0.1d')) == '2h24m'
