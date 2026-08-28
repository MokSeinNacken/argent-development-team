import math
from datetime import timedelta

_US_PER_DAY = 24 * 60 * 60 * 1000000
_US_PER_HOUR = 60 * 60 * 1000000
_US_PER_MINUTE = 60 * 1000000

# Maximum decimal places of a minute we emit.  A nonzero duration must never
# render as '0m', so the exact terminating-decimal path is kept up to this cap
# and the non-terminating fallback rounds to this many places (never to zero).
_FRACTION_CAP = 12
_FRACTION_SCALE = 10 ** _FRACTION_CAP


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
    if digits > _FRACTION_CAP:
        return None
    scaled = n * (10 ** digits) // d
    return str(scaled).rjust(digits, '0')


def _rounded_fraction(num, den):
    # Half-up round to _FRACTION_CAP decimal places of a minute.  A nonzero
    # fraction must never round to zero: at minimum emit the smallest
    # representable value (1 at the last decimal place).
    scaled = num * _FRACTION_SCALE
    q, r = divmod(scaled, den)
    if r * 2 >= den:
        q += 1
    if q == 0:
        q = 1
    return str(q).rjust(_FRACTION_CAP, '0')


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
