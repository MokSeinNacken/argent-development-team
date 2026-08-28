import re
from fractions import Fraction
from datetime import timedelta

_US_PER_DAY = 24 * 60 * 60 * 1000000
_US_PER_HOUR = 60 * 60 * 1000000
_US_PER_MINUTE = 60 * 1000000

_NUM = r'(-?\d+(?:\.\d+)?)'
_PATTERN = re.compile(
    r'^\s*'
    r'(?:' + _NUM + r'[dD]\s*)?'
    r'(?:' + _NUM + r'[hH]\s*)?'
    r'(?:' + _NUM + r'[mM]\s*)?'
    r'\s*$'
)


def _decimal_to_fraction_us(value, us_per_unit):
    whole, dot, frac = value.partition('.')
    whole_int = int(whole) if whole else 0
    if not dot:
        return Fraction(whole_int * us_per_unit, 1)
    scale = 10 ** len(frac)
    numer = (whole_int * scale + int(frac)) * us_per_unit
    return Fraction(numer, scale)


def parse_duration(s: str) -> timedelta:
    if s.strip() == '':
        raise ValueError('empty')
    match = _PATTERN.match(s)
    if not match:
        raise ValueError('invalid')
    days, hours, minutes = match.groups()
    if days is None and hours is None and minutes is None:
        raise ValueError('invalid')
    for part in (days, hours, minutes):
        if part is not None and part.startswith('-'):
            raise ValueError('negative')
    total_us = Fraction(0)
    if days is not None:
        total_us += _decimal_to_fraction_us(days, _US_PER_DAY)
    if hours is not None:
        total_us += _decimal_to_fraction_us(hours, _US_PER_HOUR)
    if minutes is not None:
        total_us += _decimal_to_fraction_us(minutes, _US_PER_MINUTE)
    whole_us = int(total_us + Fraction(1, 2))
    return timedelta(microseconds=whole_us)
