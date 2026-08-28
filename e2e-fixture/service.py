import math
from datetime import timedelta

_US_PER_DAY = 24 * 60 * 60 * 1000000
_US_PER_HOUR = 60 * 60 * 1000000
_US_PER_MINUTE = 60 * 1000000


def total_minutes(d: timedelta) -> int:
    total_us = d.days * _US_PER_DAY + d.seconds * 1000000 + d.microseconds
    return -((-total_us) // _US_PER_MINUTE)


def _fraction_decimal(num, den):
    g = math.gcd(num, den)
    n = num // g
    d = den // g
    remaining = d
    twos = 0
    while remaining % 2 == 0:
        remaining //= 2
        twos += 1
    fives = 0
    while remaining % 5 == 0:
        remaining //= 5
        fives += 1
    if remaining != 1:
        return None
    digits = max(twos, fives)
    if digits > 6:
        return None
    scaled = n * (10 ** digits) // d
    return str(scaled).rjust(digits, '0')


def _rounded_fraction(num, den):
    scaled = num * 1000000
    q, r = divmod(scaled, den)
    if r * 2 >= den:
        q += 1
    return str(q).rjust(6, '0')


def format_duration(d: timedelta) -> str:
    total_us = d.days * _US_PER_DAY + d.seconds * 1000000 + d.microseconds
    if total_us == 0:
        return '0m'
    days, rem = divmod(total_us, _US_PER_DAY)
    hours, rem = divmod(rem, _US_PER_HOUR)
    minutes, sub_us = divmod(rem, _US_PER_MINUTE)
    out = ''
    if days:
        out += '%dd' % days
    if hours:
        out += '%dh' % hours
    if sub_us == 0:
        if minutes:
            out += '%dm' % minutes
    else:
        frac = _fraction_decimal(sub_us, _US_PER_MINUTE)
        if frac is None:
            frac = _rounded_fraction(sub_us, _US_PER_MINUTE)
        frac = frac.rstrip('0')
        if frac:
            out += '%d.%sm' % (minutes, frac)
        else:
            out += '%dm' % minutes
    return out
