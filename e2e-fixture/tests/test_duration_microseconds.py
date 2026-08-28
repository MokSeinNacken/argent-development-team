from datetime import timedelta

from parser import parse_duration
from service import format_duration


def test_format_duration_microseconds_never_zero():
    # FIX 2(a): a nonzero duration must never render as '0m'.
    for us in (1, 6, 60):
        d = timedelta(microseconds=us)
        rendered = format_duration(d)
        assert rendered != '0m', f"{us}us rendered as '0m'"
        assert parse_duration(rendered) != timedelta(0), f"{us}us round-trips to 0"


def test_format_duration_exact_terminating_microseconds():
    assert format_duration(timedelta(microseconds=6)) == '0.0000001m'
    assert format_duration(timedelta(microseconds=60)) == '0.000001m'
    assert parse_duration(format_duration(timedelta(microseconds=6))) == timedelta(microseconds=6)
    assert parse_duration(format_duration(timedelta(microseconds=60))) == timedelta(microseconds=60)
