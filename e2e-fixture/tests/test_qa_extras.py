from datetime import timedelta

import pytest

from parser import parse_duration
from service import format_duration, total_minutes


def test_negative_hours():
    with pytest.raises(ValueError) as e:
        parse_duration('1d-2h')
    assert str(e.value) == 'negative'


def test_total_minutes_exact():
    assert total_minutes(timedelta(hours=1, minutes=30)) == 90


def test_format_hours_rollover():
    assert format_duration(timedelta(hours=26, minutes=3)) == '1d2h3m'
