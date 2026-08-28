from datetime import timedelta

import pytest

from parser import parse_duration
from service import format_duration, total_minutes


def test_empty():
    with pytest.raises(ValueError) as e:
        parse_duration('')
    assert str(e.value) == 'empty'


def test_whitespace():
    with pytest.raises(ValueError) as e:
        parse_duration('   ')
    assert str(e.value) == 'empty'


def test_invalid_unknown():
    with pytest.raises(ValueError) as e:
        parse_duration('1x2y')
    assert str(e.value) == 'invalid'


def test_invalid_no_unit():
    with pytest.raises(ValueError) as e:
        parse_duration('42')
    assert str(e.value) == 'invalid'


def test_invalid_bare_unit():
    with pytest.raises(ValueError) as e:
        parse_duration('d')
    assert str(e.value) == 'invalid'


def test_invalid_wrong_order():
    with pytest.raises(ValueError) as e:
        parse_duration('3m2h1d')
    assert str(e.value) == 'invalid'


def test_negative_days():
    with pytest.raises(ValueError) as e:
        parse_duration('-1d')
    assert str(e.value) == 'negative'


def test_negative_minutes():
    with pytest.raises(ValueError) as e:
        parse_duration('1d2h-3m')
    assert str(e.value) == 'negative'


def test_case_upper():
    assert parse_duration('1D2H3M') == timedelta(days=1, hours=2, minutes=3)


def test_case_mixed():
    assert parse_duration('1d2H3m') == timedelta(days=1, hours=2, minutes=3)


def test_order():
    assert parse_duration('1d2h3m') == timedelta(days=1, hours=2, minutes=3)


def test_partial():
    assert parse_duration('1d') == timedelta(days=1)
    assert parse_duration('2h') == timedelta(hours=2)
    assert parse_duration('3m') == timedelta(minutes=3)
    assert parse_duration('1d2h') == timedelta(days=1, hours=2)
    assert parse_duration('2h3m') == timedelta(hours=2, minutes=3)


def test_spaces():
    assert parse_duration(' 1d 2h 3m ') == timedelta(days=1, hours=2, minutes=3)


def test_zero_days():
    assert parse_duration('0d') == timedelta(0)


def test_zero_all():
    assert parse_duration('0d0h0m') == timedelta(0)


def test_overflow():
    assert parse_duration('1000d') == timedelta(days=1000)
    assert format_duration(timedelta(days=1000)) == '1000d'


def test_equivalence():
    a = parse_duration('1d2h3m')
    b = parse_duration('26h3m')
    assert a == b
    assert total_minutes(a) == 1563
    assert total_minutes(b) == 1563
    assert format_duration(a) == '1d2h3m'


def test_rounding():
    assert total_minutes(timedelta(0)) == 0
    assert total_minutes(timedelta(seconds=60)) == 1
    assert total_minutes(timedelta(seconds=61)) == 2
    assert total_minutes(timedelta(minutes=1, seconds=30)) == 2


def test_format_zero():
    assert format_duration(timedelta(0)) == '0m'


def test_format_omit():
    assert format_duration(timedelta(days=2)) == '2d'
    assert format_duration(timedelta(hours=2)) == '2h'
    assert format_duration(timedelta(minutes=5)) == '5m'
    assert format_duration(timedelta(days=1, minutes=5)) == '1d5m'
